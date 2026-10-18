"""ZAI serverless gateway: RunPod-compatible queue endpoints on the shared GPU.

Two listeners:

  :8080  public, routed by the hub's proxy at /v2/ (c.Proxy.extra_routes):
         POST /v2/<endpoint>/run | /runsync      submit a job
         GET  /v2/<endpoint>/status/<job>        result or state
         GET  /v2/<endpoint>/stream/<job>        streamed chunks not yet read
         POST /v2/<endpoint>/cancel/<job>, /purge-queue, GET /health
         GET|POST /v2/endpoints, DELETE /v2/endpoints/<id>   manage endpoints
         Authorization: Bearer <JupyterHub token> -- checked against the hub.

  :8081  internal, reachable only from worker pods (NetworkPolicy): the
         RunPod worker protocol (job-take, job-done, job-stop, ping), so
         handlers written for runpod.serverless.start() run unchanged.

A worker is a pod in the hub's namespace running a curated template image
with the endpoint owner's home volume mounted; the handler code lives there.
Every worker gets its GPU and its billing session from the hub
(/hub/api/zai/workers/open) under the same credit and capacity checks as a
notebook, and is charged by the second from pod creation to deletion.

State is one SQLite file. Job inputs and outputs are deleted RESULT_TTL after
a job finishes (SYNC_RESULT_TTL for /runsync); tokens are never stored, only
SHA-256 hashes of worker secrets.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import ssl
import string
import time
import uuid
from urllib.parse import quote, urlencode

import yaml
from tornado import httpclient, web
from tornado.httpserver import HTTPServer

log = logging.getLogger("zai-serverless")


def _env(name, default, cast=str):
    v = os.environ.get(name)
    return cast(v) if v not in (None, "") else default


DB_PATH = _env("GW_DB", "/data/gateway.sqlite")
CATALOG_PATH = _env("ZAI_CATALOG", "/etc/zai/catalog.yaml")
HUB_API = _env("HUB_API", "http://hub:8081/hub/api").rstrip("/")
HUB_TOKEN = _env("HUB_TOKEN", "")
K8S_API = _env("K8S_API", "https://kubernetes.default.svc").rstrip("/")
K8S_TOKEN_FILE = _env("K8S_TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token")
K8S_CA = _env("K8S_CA", "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
NAMESPACE = _env("NAMESPACE", "jupyterhub")
INTERNAL_URL = _env("INTERNAL_URL", "http://serverless-gateway-internal.jupyterhub.svc:8081").rstrip("/")
RUNTIME_CLASS = _env("RUNTIME_CLASS", "nvidia")
PULL_POLICY = _env("IMAGE_PULL_POLICY", "Never")
RUNTIME_CONFIGMAP = _env("RUNTIME_CONFIGMAP", "serverless-runtime")
SHARED_DATASETS = _env("SHARED_DATASETS", "/srv/jupyter-shared/datasets")

TICK = _env("TICK_SECONDS", 2.0, float)
START_TIMEOUT = _env("WORKER_START_TIMEOUT", 900, int)      # pod created -> first job-take
RESULT_TTL = _env("RESULT_TTL", 1800, int)                  # RunPod: 30 min for /run
SYNC_RESULT_TTL = _env("SYNC_RESULT_TTL", 60, int)          # RunPod: 1 min for /runsync
RUNSYNC_WAIT = _env("RUNSYNC_WAIT", 90, int)                # under Cloudflare's ~120 s cut
DEFAULT_TTL = _env("DEFAULT_JOB_TTL", 86400, int)           # queued jobs expire after this
MAX_INPUT = _env("MAX_INPUT_BYTES", 10 * 1024 * 1024, int)
MAX_OUTPUT = _env("MAX_OUTPUT_BYTES", 20 * 1024 * 1024, int)
MAX_QUEUE = _env("MAX_QUEUE_PER_ENDPOINT", 1000, int)
MAX_STREAM = _env("MAX_STREAM_BYTES", MAX_OUTPUT, int)          # per job, all chunks together
MAX_STORED = _env("MAX_STORED_BYTES_PER_OWNER", 500 * 1024 * 1024, int)   # outputs + streams kept
LONG_POLL = _env("LONG_POLL_SECONDS", 20, int)
CREDIT_EVERY = _env("CREDIT_CHECK_SECONDS", 15, int)
AUTH_CACHE = _env("AUTH_CACHE_SECONDS", 60, int)

INVOKE_SCOPE = "custom:serverless"     # granted to the user role (charts/jupyterhub-values.yaml)

TERMINAL = ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT")
EP_RE = re.compile(r"^[a-z0-9]{12}$")
JOB_RE = re.compile(r"^[0-9a-f-]{36}$")
WORKER_RE = re.compile(r"^[a-z0-9]{12}-[a-z0-9]{6}$")
USER_RE = re.compile(r"^[a-z][a-z0-9-]{2,30}$")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
WORKDIR_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,62}$")


def _rand(n):
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


# Container waiting reasons that retrying will not fix: with pullPolicy Never a
# missing image stays missing (kubelet's image GC removes unused ones when the
# disk is full), and a bad spec stays bad.
UNSTARTABLE = {"ErrImageNeverPull", "ErrImagePull", "ImagePullBackOff", "InvalidImageName",
               "CreateContainerConfigError", "CreateContainerError", "RunContainerError"}


def _unstartable(pod):
    """The reason a pod will never start, or None."""
    st = pod.get("status", {})
    if pod["metadata"].get("deletionTimestamp") or st.get("phase") != "Pending":
        return None
    for cs in st.get("containerStatuses") or []:
        reason = (cs.get("state", {}).get("waiting") or {}).get("reason")
        if reason in UNSTARTABLE:
            return reason
    return None


def _int_arg(handler, name, default):
    v = handler.get_argument(name, None)
    if v is None:
        return default
    if not re.fullmatch(r"\d{1,9}", v):
        raise web.HTTPError(400, reason=f"{name}: a whole number")
    return int(v)


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def _reject_constant(c):
    raise ValueError(f"{c} is not valid JSON")


def strict_json(raw):
    """Parse JSON the way the API promises: UTF-8, no NaN/Infinity."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw, parse_constant=_reject_constant)


def compact(v):
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


# ---------------------------------------------------------------------------
# Catalog (the hub's catalog.yaml, mounted read-only)
# ---------------------------------------------------------------------------

class Catalog:
    def __init__(self, path=CATALOG_PATH):
        with open(path) as f:
            d = yaml.safe_load(f) or {}
        sv = d.get("serverless") or {}
        self.enabled = bool(sv.get("enabled", False))
        self.max_workers = int(sv.get("max_workers_per_endpoint", 3))
        self.sizes = {s["slug"]: s for s in d.get("sizes") or []}
        self.templates = {t["slug"]: t for t in d.get("templates") or []}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.con = sqlite3.connect(path, isolation_level=None)   # autocommit; explicit BEGIN where needed
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA foreign_keys=ON")
        self.con.executescript("""
            CREATE TABLE IF NOT EXISTS endpoints (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
                size TEXT NOT NULL, template TEXT NOT NULL,
                workdir TEXT NOT NULL, command TEXT NOT NULL, env TEXT NOT NULL DEFAULT '{}',
                max_workers INTEGER NOT NULL, idle_timeout INTEGER NOT NULL,
                exec_timeout INTEGER NOT NULL, created REAL NOT NULL, created_by TEXT NOT NULL,
                deleted REAL, last_error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, endpoint TEXT NOT NULL REFERENCES endpoints(id),
                owner TEXT NOT NULL, status TEXT NOT NULL, input TEXT NOT NULL,
                output TEXT, error TEXT, created REAL NOT NULL, started REAL, finished REAL,
                worker TEXT, ttl_at REAL NOT NULL, exec_timeout INTEGER NOT NULL,
                retries INTEGER NOT NULL DEFAULT 0, sync INTEGER NOT NULL DEFAULT 0,
                purge_at REAL);
            CREATE INDEX IF NOT EXISTS ix_jobs_queue ON jobs(endpoint, status, created);
            CREATE INDEX IF NOT EXISTS ix_jobs_purge ON jobs(purge_at);
            CREATE INDEX IF NOT EXISTS ix_jobs_owner ON jobs(owner);
            CREATE TABLE IF NOT EXISTS stream (
                job TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL, chunk TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (job, seq));
            CREATE TABLE IF NOT EXISTS workers (
                id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, owner TEXT NOT NULL,
                secret_hash TEXT NOT NULL, state TEXT NOT NULL,
                created REAL NOT NULL, last_active REAL NOT NULL, first_take REAL,
                session_open INTEGER NOT NULL DEFAULT 0, stop_reason TEXT NOT NULL DEFAULT '',
                gone_at REAL);
            CREATE INDEX IF NOT EXISTS ix_workers_state ON workers(state);
        """)

    def q(self, sql, args=()):
        return self.con.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.con.execute(sql, args).fetchone()

    def x(self, sql, args=()):
        return self.con.execute(sql, args)


# ---------------------------------------------------------------------------
# Kubernetes and hub clients (plain REST; tornado is the only dependency)
# ---------------------------------------------------------------------------

class K8s:
    def __init__(self):
        self.http = httpclient.AsyncHTTPClient()
        self.ssl = None
        if K8S_API.startswith("https") and os.path.exists(K8S_CA):
            self.ssl = ssl.create_default_context(cafile=K8S_CA)

    def _headers(self):
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if os.path.exists(K8S_TOKEN_FILE):
            with open(K8S_TOKEN_FILE) as f:          # re-read: projected tokens rotate
                h["Authorization"] = "Bearer " + f.read().strip()
        return h

    async def req(self, method, path, body=None, ok=(200, 201, 202)):
        r = await self.http.fetch(
            K8S_API + path, method=method, headers=self._headers(),
            body=compact(body) if body is not None else None,
            allow_nonstandard_methods=True, raise_error=False, ssl_options=self.ssl, request_timeout=30)
        if r.code not in ok:
            raise RuntimeError(f"k8s {method} {path}: {r.code} {r.body[:300] if r.body else ''}")
        return json.loads(r.body) if r.body else {}

    async def worker_pods(self):
        sel = quote("component=serverless-worker")
        d = await self.req("GET", f"/api/v1/namespaces/{NAMESPACE}/pods?labelSelector={sel}")
        return d.get("items", [])

    async def create_pod(self, spec):
        return await self.req("POST", f"/api/v1/namespaces/{NAMESPACE}/pods", spec)

    async def delete_pod(self, name, grace=10):
        await self.req("DELETE", f"/api/v1/namespaces/{NAMESPACE}/pods/{name}?gracePeriodSeconds={grace}",
                       ok=(200, 202, 404))

    async def home_pvc(self, owner):
        """The owner's home volume, found by the annotation KubeSpawner puts on
        it. Not f"claim-{owner}": KubeSpawner hashes some usernames (abc- ->
        claim-abc---2211b74f), so a user registered as that hashed form would
        otherwise get the other person's home mounted."""
        d = await self.req("GET", f"/api/v1/namespaces/{NAMESPACE}/persistentvolumeclaims")
        names = sorted(p["metadata"]["name"] for p in d.get("items", [])
                       if (p["metadata"].get("annotations") or {}).get("hub.jupyter.org/username") == owner
                       and not p["metadata"].get("deletionTimestamp"))
        return names[0] if len(names) == 1 else None


class HubError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Hub:
    def __init__(self):
        self.http = httpclient.AsyncHTTPClient()
        self._auth = {}       # sha256(token) -> (expiry, user model); never the token

    async def _call(self, method, path, token, body=None):
        r = await self.http.fetch(
            HUB_API + path, method=method, raise_error=False, request_timeout=20,
            headers={"Authorization": "token " + token, "Content-Type": "application/json"},
            body=compact(body) if body is not None else None)
        data = {}
        if r.body:
            try:
                data = json.loads(r.body)
            except ValueError:
                pass
        if r.code >= 400 or r.code == 599:
            raise HubError(r.code, data.get("message") or f"hub returned {r.code}")
        return data

    async def whoami(self, token):
        key = _sha(token)
        hit = self._auth.get(key)
        now = time.time()
        if hit and hit[0] > now:
            return hit[1]
        try:
            u = await self._call("GET", "/user", token)
        except HubError as e:
            if e.code in (401, 403, 404):
                return None
            raise
        if u.get("kind", "user") != "user" or not u.get("name"):
            return None
        # Only tokens carrying the serverless scope: a user's own API tokens
        # inherit it from the user role, but OAuth tokens that a shared server
        # or app receives for its visitors do not -- else alice, sharing her
        # server with bob, could call bob's endpoints with his token.
        model = {"name": u["name"], "admin": bool(u.get("admin")),
                 "serverless": INVOKE_SCOPE in (u.get("scopes") or [])}
        if len(self._auth) > 10000:
            self._auth.clear()
        self._auth[key] = (now + AUTH_CACHE, model)
        return model

    async def open_worker(self, worker, user, size, endpoint, name=""):
        return await self._call("POST", "/zai/workers/open", HUB_TOKEN,
                                {"worker": worker, "user": user, "size": size, "endpoint": endpoint,
                                 "endpoint_name": name})

    async def close_worker(self, worker):
        return await self._call("POST", "/zai/workers/close", HUB_TOKEN, {"worker": worker})

    async def accounts(self, users):
        qs = urlencode([("user", u) for u in users])
        return await self._call("GET", f"/zai/workers/accounts?{qs}", HUB_TOKEN)


# ---------------------------------------------------------------------------
# The gateway
# ---------------------------------------------------------------------------

class Gateway:
    def __init__(self, store=None, k8s=None, hub=None, catalog=None):
        self.db = store or Store()
        self.k8s = k8s or K8s()
        self.hub = hub or Hub()
        self.catalog = catalog or Catalog()
        self.job_events = {}        # job id -> asyncio.Event, set when it finishes
        self.queue_events = {}      # endpoint -> asyncio.Event, set when a job is queued
        self.stop_events = {}       # worker -> asyncio.Event, set when it must stop a job
        self.accounts = {}          # owner -> {"balance", "free"}; refreshed every CREDIT_EVERY
        self.enforce = False
        self._last_credit = 0.0
        self._starting = set()      # endpoints with a worker start in flight

    # ---- helpers -------------------------------------------------------

    def _event(self, table, key):
        ev = table.get(key)
        if ev is None:
            ev = table[key] = asyncio.Event()
        return ev

    def _signal(self, table, key):
        ev = table.pop(key, None)
        if ev:
            ev.set()

    def finish_job(self, job_id, status, output=None, error=None):
        now = time.time()
        row = self.db.one("SELECT sync FROM jobs WHERE id=?", (job_id,))
        if not row:
            return
        ttl = SYNC_RESULT_TTL if row["sync"] else RESULT_TTL
        self.db.x("UPDATE jobs SET status=?, output=?, error=?, finished=?, purge_at=? "
                  "WHERE id=? AND status NOT IN ('COMPLETED','FAILED','CANCELLED','TIMED_OUT')",
                  (status, output, error, now, now + ttl, job_id))
        self._signal(self.job_events, job_id)

    def job_view(self, j):
        out = {"id": j["id"], "status": j["status"]}
        if j["started"]:
            out["delayTime"] = int((j["started"] - j["created"]) * 1000)
        if j["finished"] and j["started"]:
            out["executionTime"] = int((j["finished"] - j["started"]) * 1000)
        if j["worker"]:
            out["workerId"] = j["worker"]
        if j["output"] is not None:
            out["output"] = json.loads(j["output"])
        if j["error"] is not None:
            out["error"] = j["error"]
        return out

    # ---- endpoints -----------------------------------------------------

    def validate_endpoint(self, b, creating=True):
        def need(cond, msg):
            if not cond:
                raise ValueError(msg)
        allowed = {"owner", "name", "size", "template", "workdir", "command", "env",
                   "max_workers", "idle_timeout", "execution_timeout"}
        extra = set(b) - allowed
        need(not extra, f"unknown field(s): {', '.join(sorted(extra))}")
        need(isinstance(b.get("owner"), str) and USER_RE.match(b["owner"]), "owner: a username")
        need(isinstance(b.get("name"), str) and NAME_RE.match(b["name"]), "name: 1-63 letters, digits, space . _ -")
        size = self.catalog.sizes.get(b.get("size"))
        need(size is not None, f"size: one of {', '.join(self.catalog.sizes)}")
        tpl = self.catalog.templates.get(b.get("template", "pytorch"))
        need(tpl is not None, f"template: one of {', '.join(self.catalog.templates)}")
        need(size.get("gpu") or not tpl.get("gpu_required"), f"template {tpl['slug']} needs a GPU size")
        workdir = b.get("workdir", "")
        need(isinstance(workdir, str) and len(workdir) <= 200 and WORKDIR_RE.match(workdir)
             and ".." not in workdir.split("/"), "workdir: a path inside the owner's home, e.g. endpoints/echo")
        cmd = b.get("command", "python handler.py")
        need(isinstance(cmd, str) and 0 < len(cmd) <= 1000 and "\n" not in cmd and "\x00" not in cmd,
             "command: one line, up to 1000 characters")
        env = b.get("env", {})
        need(isinstance(env, dict) and len(env) <= 50, "env: an object of up to 50 variables")
        for k, v in env.items():
            need(isinstance(k, str) and ENV_RE.match(k), f"env name {k!r}: A-Z, 0-9, _")
            # NVIDIA_*/CUDA_*: device selection (NVIDIA_VISIBLE_DEVICES=all would
            # reach other tenants' MIG slices). LD_*: loader hijacking.
            need(not k.startswith(("RUNPOD_", "ZAI_", "NVIDIA_", "CUDA_", "LD_"))
                 and k not in ("HOME", "PATH", "PYTHONPATH"), f"env {k} is reserved")
            need(isinstance(v, str) and len(v) <= 4096 and "\x00" not in v, f"env {k}: a string up to 4096 chars")
        mw = b.get("max_workers", 1)
        need(isinstance(mw, int) and 1 <= mw <= self.catalog.max_workers,
             f"max_workers: 1-{self.catalog.max_workers}")
        it = b.get("idle_timeout", 30)
        need(isinstance(it, int) and 5 <= it <= 3600, "idle_timeout: 5-3600 seconds")
        et = b.get("execution_timeout", 600)
        need(isinstance(et, int) and 1 <= et <= 86400, "execution_timeout: 1-86400 seconds")
        return {"owner": b["owner"], "name": b["name"].strip(), "size": size["slug"], "template": tpl["slug"],
                "workdir": workdir, "command": cmd, "env": compact(env), "max_workers": mw,
                "idle_timeout": it, "exec_timeout": et}

    def endpoint_view(self, e, detail=True):
        workers = self.db.q("SELECT state FROM workers WHERE endpoint=? AND state!='gone'", (e["id"],))
        out = {"id": e["id"], "name": e["name"], "owner": e["owner"], "size": e["size"],
               "template": e["template"], "maxWorkers": e["max_workers"], "idleTimeout": e["idle_timeout"],
               "executionTimeout": e["exec_timeout"], "workers": len(workers)}
        if detail:
            out.update({"workdir": e["workdir"], "command": e["command"],
                        "env": sorted(json.loads(e["env"]))})   # names only: values may be secrets
        return out

    # ---- jobs ----------------------------------------------------------

    def stored_bytes(self, owner):
        out = self.db.one("SELECT COALESCE(SUM(LENGTH(output)),0) FROM jobs WHERE owner=?", (owner,))[0]
        st = self.db.one("SELECT COALESCE(SUM(LENGTH(s.chunk)),0) FROM stream s JOIN jobs j ON j.id=s.job "
                         "WHERE j.owner=?", (owner,))[0]
        return out + st

    def submit(self, ep, body, sync):
        if not isinstance(body, dict):
            raise web.HTTPError(400, reason="body must be a JSON object")
        extra = set(body) - {"input", "policy"}
        if "webhook" in extra:
            raise web.HTTPError(400, reason="webhook is not supported yet; poll /status instead")
        if extra:
            raise web.HTTPError(400, reason=f"unknown field(s): {', '.join(sorted(extra))}")
        if "input" not in body:
            raise web.HTTPError(400, reason="missing input")
        pol = body.get("policy") or {}
        if not isinstance(pol, dict) or set(pol) - {"executionTimeout", "ttl"}:
            raise web.HTTPError(400, reason="policy supports executionTimeout and ttl (milliseconds)")

        def ms(key, default_s, lo, hi):
            v = pol.get(key)
            if v is None:
                return default_s
            if not isinstance(v, int) or isinstance(v, bool) or not lo * 1000 <= v <= hi * 1000:
                raise web.HTTPError(400, reason=f"policy.{key}: {lo * 1000}-{hi * 1000} ms")
            return math.ceil(v / 1000)

        exec_to = min(ms("executionTimeout", ep["exec_timeout"], 1, 86400), ep["exec_timeout"])
        ttl = ms("ttl", DEFAULT_TTL, 10, 7 * 86400)
        queued = self.db.one("SELECT COUNT(*) FROM jobs WHERE endpoint=? AND status='IN_QUEUE'", (ep["id"],))[0]
        if queued >= MAX_QUEUE:
            raise web.HTTPError(429, reason=f"queue is full ({MAX_QUEUE} jobs)")
        acct = self.accounts.get(ep["owner"])
        if self.enforce and acct and not acct["free"] and acct["balance"] <= 0:
            raise web.HTTPError(402, reason="Not enough credit. Request more credit on your Account page.")
        now = time.time()
        job_id = str(uuid.uuid4())
        self.db.x("INSERT INTO jobs(id, endpoint, owner, status, input, created, ttl_at, exec_timeout, sync)"
                  " VALUES (?,?,?,?,?,?,?,?,?)",
                  (job_id, ep["id"], ep["owner"], "IN_QUEUE", compact(body["input"]), now, now + ttl,
                   exec_to, int(sync)))
        self._signal(self.queue_events, ep["id"])
        return job_id

    def take(self, worker, limit=1):
        """Atomically hand the oldest queued jobs of the worker's endpoint to it."""
        now = time.time()
        self.db.x("BEGIN IMMEDIATE")
        try:
            rows = self.db.q("SELECT id, input FROM jobs WHERE endpoint=? AND status='IN_QUEUE' "
                             "ORDER BY created LIMIT ?", (worker["endpoint"], limit))
            for r in rows:
                self.db.x("UPDATE jobs SET status='IN_PROGRESS', started=?, worker=? WHERE id=?",
                          (now, worker["id"], r["id"]))
            if rows:
                self.db.x("UPDATE workers SET state='busy', last_active=?, first_take=COALESCE(first_take, ?) "
                          "WHERE id=?", (now, now, worker["id"]))
            else:
                self.db.x("UPDATE workers SET first_take=COALESCE(first_take, ?), "
                          "state=CASE WHEN state='starting' THEN 'idle' ELSE state END WHERE id=?",
                          (now, worker["id"]))
            self.db.x("COMMIT")
        except Exception:
            self.db.x("ROLLBACK")
            raise
        return [{"id": r["id"], "input": json.loads(r["input"])} for r in rows]

    # ---- workers -------------------------------------------------------

    def pod_spec(self, ep, wid, secret, pvc):
        size = self.catalog.sizes[ep["size"]]
        tpl = self.catalog.templates[ep["template"]]
        gpu = size.get("gpu")
        limits = {"cpu": str(size.get("cpu", 2)), "memory": str(size.get("memory", "8G"))}
        if gpu:
            limits[gpu["resource"]] = str(gpu.get("count", 1))
        requests = {"cpu": str(size.get("cpu_guarantee", 0.5)), "memory": str(size.get("memory_guarantee", "1G"))}
        base = INTERNAL_URL + "/internal"
        env = {
            "RUNPOD_POD_ID": wid,
            "RUNPOD_ENDPOINT_ID": ep["id"],
            "RUNPOD_AI_API_KEY": secret,
            # The SDK appends "&job_in_progress=..." / "&isStream=...", so each
            # URL already carries a query string.
            "RUNPOD_WEBHOOK_GET_JOB": f"{base}/job-take/$ID?v=1",
            "RUNPOD_WEBHOOK_POST_OUTPUT": f"{base}/job-done/$RUNPOD_POD_ID?id=$ID",
            "RUNPOD_WEBHOOK_POST_STREAM": f"{base}/job-stream/$RUNPOD_POD_ID?id=$ID",
            "RUNPOD_WEBHOOK_PING": f"{base}/ping/$RUNPOD_POD_ID",
            "RUNPOD_PING_INTERVAL": "10000",
            "ZAI_ENDPOINT": ep["id"],
            "JUPYTERHUB_USER": ep["owner"],
            "HOME": "/home/jovyan",
            "PYTHONPATH": "/opt/zai-runtime",
            "PYTHONUNBUFFERED": "1",
        }
        env.update(json.loads(ep["env"]))
        workdir = "/home/jovyan/" + ep["workdir"]
        spec = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": f"sw-{wid}",
                "labels": {"component": "serverless-worker", "zai/endpoint": ep["id"],
                           "zai/worker": wid, "zai/owner": ep["owner"]},
            },
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "enableServiceLinks": False,
                "terminationGracePeriodSeconds": 10,
                "securityContext": {"runAsUser": 1000, "runAsGroup": 100, "fsGroup": 100,
                                    "runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{
                    "name": "worker",
                    "image": tpl["image"],
                    "imagePullPolicy": PULL_POLICY,
                    "workingDir": workdir,
                    "command": ["bash", "-c", "exec " + ep["command"]],
                    "env": [{"name": k, "value": v} for k, v in env.items()],
                    "resources": {"limits": limits, "requests": requests},
                    "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                    "volumeMounts": [
                        {"name": "home", "mountPath": "/home/jovyan"},
                        {"name": "runtime", "mountPath": "/opt/zai-runtime", "readOnly": True},
                        {"name": "shared", "mountPath": "/home/jovyan/shared", "readOnly": True},
                        {"name": "dshm", "mountPath": "/dev/shm"},
                        {"name": "tmp", "mountPath": "/tmp"},
                    ],
                }],
                "volumes": [
                    {"name": "home", "persistentVolumeClaim": {"claimName": pvc}},
                    {"name": "runtime", "configMap": {"name": RUNTIME_CONFIGMAP}},
                    {"name": "shared", "hostPath": {"path": SHARED_DATASETS, "type": "DirectoryOrCreate"}},
                    {"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": "2Gi"}},
                    {"name": "tmp", "emptyDir": {"sizeLimit": "20Gi"}},
                ],
            },
        }
        if size.get("gpu"):
            rc = size.get("runtime_class") or RUNTIME_CLASS
            if rc:
                spec["spec"]["runtimeClassName"] = rc
        else:
            # Without a GPU, no nvidia runtime and no devices: the images set
            # NVIDIA_VISIBLE_DEVICES=all, which would expose every MIG slice.
            for c in spec["spec"]["containers"]:
                c["env"] = [e for e in c["env"] if e["name"] != "NVIDIA_VISIBLE_DEVICES"] + \
                    [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"}]
        if size.get("node_selector"):
            spec["spec"]["nodeSelector"] = size["node_selector"]
        return spec

    async def start_worker(self, ep):
        wid = f"{ep['id']}-{_rand(6)}"
        try:
            pvc = await self.k8s.home_pvc(ep["owner"])
        except Exception as e:
            log.warning("looking up %s's home volume: %s", ep["owner"], e)
            return None
        if not pvc:
            self.db.x("UPDATE endpoints SET last_error=? WHERE id=?",
                      (f"{ep['owner']} has no home volume (or more than one)", ep["id"]))
            return None
        try:
            await self.hub.open_worker(wid, ep["owner"], ep["size"], ep["id"], ep["name"])
        except HubError as e:
            # 409/503: every slice is busy, so jobs wait for one -- not an error.
            if e.code not in (409, 503):
                self.db.x("UPDATE endpoints SET last_error=? WHERE id=?", (str(e)[:300], ep["id"]))
            if e.code == 402:
                # Out of credit: nothing will ever run, so say so rather than
                # letting jobs sit until their TTL.
                for j in self.db.q("SELECT id FROM jobs WHERE endpoint=? AND status='IN_QUEUE'", (ep["id"],)):
                    self.finish_job(j["id"], "FAILED", error="Not enough credit to start a worker.")
            log.info("worker for %s not admitted: %s %s", ep["id"], e.code, e)
            return None
        secret = secrets.token_urlsafe(32)
        now = time.time()
        self.db.x("INSERT INTO workers(id, endpoint, owner, secret_hash, state, created, last_active, session_open)"
                  " VALUES (?,?,?,?,?,?,?,1)", (wid, ep["id"], ep["owner"], _sha(secret), "starting", now, now))
        try:
            await self.k8s.create_pod(self.pod_spec(ep, wid, secret, pvc))
        except Exception as e:
            log.error("pod create failed for %s: %s", wid, e)
            self.db.x("UPDATE endpoints SET last_error=? WHERE id=?", (f"pod create failed: {e}"[:300], ep["id"]))
            self.db.x("UPDATE workers SET state='gone', stop_reason='create failed', gone_at=? WHERE id=?",
                      (time.time(), wid))
            await self.close_session(wid)
            return None
        self.db.x("UPDATE endpoints SET last_error='' WHERE id=?", (ep["id"],))
        log.info("worker %s started for endpoint %s (%s)", wid, ep["id"], ep["owner"])
        return wid

    async def stop_worker(self, wid, reason):
        self.db.x("UPDATE workers SET state='stopping', stop_reason=? WHERE id=? AND state NOT IN ('stopping','gone')",
                  (reason, wid))
        try:
            await self.k8s.delete_pod(f"sw-{wid}")
        except Exception as e:
            log.warning("delete pod sw-%s: %s", wid, e)

    async def close_session(self, wid):
        try:
            await self.hub.close_worker(wid)
            self.db.x("UPDATE workers SET session_open=0 WHERE id=?", (wid,))
        except Exception as e:
            log.warning("hub close for %s failed (will retry): %s", wid, e)

    def worker_gone(self, w, why):
        """A worker's pod no longer runs: requeue or fail its job, mark it gone."""
        for j in self.db.q("SELECT id, retries FROM jobs WHERE worker=? AND status='IN_PROGRESS'", (w["id"],)):
            # The job that caused a timeout/cancel/credit stop is already
            # terminal, so not selected here; any others (batch) still need it.
            if j["retries"] < 1:
                self.db.x("UPDATE jobs SET status='IN_QUEUE', started=NULL, worker=NULL, retries=retries+1 "
                          "WHERE id=?", (j["id"],))
                self._signal(self.queue_events, w["endpoint"])
            else:
                self.finish_job(j["id"], "FAILED", error=f"worker stopped while running the job ({why})")
        self.db.x("UPDATE workers SET state='gone', gone_at=? WHERE id=?", (time.time(), w["id"]))
        self._signal(self.stop_events, w["id"])

    # ---- the control loop ---------------------------------------------

    async def tick(self):
        now = time.time()
        pods = {p["metadata"]["labels"].get("zai/worker"): p for p in await self.k8s.worker_pods()}

        # 1. reconcile workers with pods
        for w in self.db.q("SELECT * FROM workers WHERE state!='gone'"):
            p = pods.get(w["id"])
            phase = (p or {}).get("status", {}).get("phase")
            if p is None or phase in ("Failed", "Succeeded"):
                if p is not None:
                    await self.k8s.delete_pod(f"sw-{w['id']}", grace=0)
                self.worker_gone(w, "exited" if p is not None else "pod deleted")
                await self.close_session(w["id"])
            elif (stuck := _unstartable(p)):
                # Otherwise it sits until WORKER_START_TIMEOUT, holding a GPU
                # cap slot and billing its owner, while the jobs never run.
                why = f"worker could not start: {stuck}"
                log.warning("worker %s: %s", w["id"], why)
                await self.k8s.delete_pod(f"sw-{w['id']}", grace=0)
                self.worker_gone(w, why)
                await self.close_session(w["id"])
                self.db.x("UPDATE endpoints SET last_error=? WHERE id=?", (why[:300], w["endpoint"]))
                for j in self.db.q("SELECT id FROM jobs WHERE endpoint=? AND status='IN_QUEUE'", (w["endpoint"],)):
                    self.finish_job(j["id"], "FAILED", error=why)
        # A stop whose pod delete failed (API hiccup) would leave the owner's
        # code holding the GPU, unbilled after the credit stop: retry it.
        for w in self.db.q("SELECT id FROM workers WHERE state='stopping'"):
            p = pods.get(w["id"])
            if p is not None and not p["metadata"].get("deletionTimestamp"):
                try:
                    await self.k8s.delete_pod(f"sw-{w['id']}")
                except Exception as e:
                    log.warning("retrying delete of sw-%s: %s", w["id"], e)
        known = {r["id"] for r in self.db.q("SELECT id FROM workers WHERE state!='gone'")}
        for wid, p in pods.items():
            if wid not in known and not p["metadata"].get("deletionTimestamp"):
                log.warning("deleting orphan worker pod %s", p["metadata"]["name"])
                await self.k8s.delete_pod(p["metadata"]["name"], grace=0)
                await self.close_session(wid)
        for w in self.db.q("SELECT id FROM workers WHERE state='gone' AND session_open=1"):
            await self.close_session(w["id"])

        # 2. credit
        if now - self._last_credit >= CREDIT_EVERY:
            owners = [r["owner"] for r in self.db.q(
                "SELECT DISTINCT owner FROM workers WHERE state!='gone' UNION "
                "SELECT DISTINCT owner FROM jobs WHERE status IN ('IN_QUEUE','IN_PROGRESS') UNION "
                "SELECT DISTINCT owner FROM endpoints WHERE deleted IS NULL")]
            if owners:
                try:
                    a = await self.hub.accounts(owners)
                    self.enforce = bool(a["enforce"])
                    self.accounts = a["accounts"]
                    self._last_credit = now
                except Exception as e:
                    log.warning("hub accounts: %s", e)
            if self.enforce:
                for owner, acct in self.accounts.items():
                    if acct["free"] or acct["balance"] > 0:
                        continue
                    for j in self.db.q("SELECT id FROM jobs WHERE owner=? AND status IN ('IN_QUEUE','IN_PROGRESS')",
                                       (owner,)):
                        self.finish_job(j["id"], "FAILED", error="Credit ran out.")
                    for w in self.db.q("SELECT id FROM workers WHERE owner=? AND state NOT IN ('stopping','gone')",
                                       (owner,)):
                        log.info("stopping worker %s: %s is out of credit", w["id"], owner)
                        await self.stop_worker(w["id"], "credit")

        # 3. timeouts
        for j in self.db.q("SELECT id, worker FROM jobs WHERE status='IN_PROGRESS' AND started + exec_timeout < ?",
                           (now,)):
            self.finish_job(j["id"], "TIMED_OUT", error="executionTimeout exceeded")
            if j["worker"]:
                await self.stop_worker(j["worker"], "timeout")
        for j in self.db.q("SELECT id FROM jobs WHERE status='IN_QUEUE' AND ttl_at < ?", (now,)):
            self.finish_job(j["id"], "TIMED_OUT", error="ttl expired before a worker took the job")
        for w in self.db.q("SELECT id FROM workers WHERE state='starting' AND created < ?", (now - START_TIMEOUT,)):
            await self.stop_worker(w["id"], "never became ready")

        # 4. scale
        for ep in self.db.q("SELECT * FROM endpoints WHERE deleted IS NULL"):
            demand = self.db.one("SELECT COUNT(*) FROM jobs WHERE endpoint=? AND status IN ('IN_QUEUE','IN_PROGRESS')",
                                 (ep["id"],))[0]
            queued = self.db.one("SELECT COUNT(*) FROM jobs WHERE endpoint=? AND status='IN_QUEUE'", (ep["id"],))[0]
            active = self.db.q("SELECT * FROM workers WHERE endpoint=? AND state IN ('starting','idle','busy')",
                               (ep["id"],))
            idle_or_starting = sum(1 for w in active if w["state"] in ("starting", "idle"))
            want = min(ep["max_workers"], demand)
            if queued > idle_or_starting and len(active) < want and ep["id"] not in self._starting:
                self._starting.add(ep["id"])
                try:
                    await self.start_worker(ep)
                finally:
                    self._starting.discard(ep["id"])
            # idle reap: nothing queued and idle longer than the endpoint allows
            if queued == 0:
                for w in active:
                    if w["state"] == "idle" and now - w["last_active"] > ep["idle_timeout"]:
                        await self.stop_worker(w["id"], "idle")

        # 5. purge: inputs/outputs go when their result TTL ends
        self.db.x("DELETE FROM jobs WHERE purge_at IS NOT NULL AND purge_at < ?", (now,))
        self.db.x("DELETE FROM workers WHERE state='gone' AND session_open=0 AND gone_at < ?", (now - 86400,))

    async def run_loop(self):
        last_trace = 0.0
        while True:
            try:
                await self.tick()
            except Exception as e:
                # Full traceback at most once a minute: an API outage would
                # otherwise log one every tick.
                if time.time() - last_trace > 60:
                    log.exception("control loop")
                    last_trace = time.time()
                else:
                    log.warning("control loop: %s", e)
            await asyncio.sleep(TICK)


# ---------------------------------------------------------------------------
# HTTP: public API
# ---------------------------------------------------------------------------

@web.stream_request_body
class Base(web.RequestHandler):
    """Bodies are streamed so an oversized one gets a clean 413 with a reason.
    With tornado's own max_body_size the connection is just dropped, which a
    client (or a worker posting a result) sees as a network error and retries."""

    LIMIT = MAX_INPUT
    AUTH_FIRST = True       # customers: check the token before reading a byte of body

    def initialize(self, gw, via_hub=False):
        self.gw = gw
        self.via_hub = via_hub
        self._chunks, self._size, self._too_big = [], 0, False
        self._user = None
        self._refused = None

    async def prepare(self):
        n = self.request.headers.get("Content-Length")
        if n and n.isdigit() and int(n) > self.LIMIT:
            self._too_big = True
        if self.AUTH_FIRST:
            # Bodies are streamed in after prepare(): without this, anyone could
            # make the gateway buffer 10 MB per connection before being refused.
            try:
                self._user = await self.user()
            except web.HTTPError as e:
                if self._too_big:
                    raise
                # Answering before the body is read makes tornado close the
                # connection mid-upload; the hub proxy then sees ECONNRESET and
                # serves its own 503 page. Discard the body, then refuse.
                self._refused = e
                setattr(self, self.request.method.lower(), self._refuse)

    async def _refuse(self, *args, **kwargs):
        raise self._refused

    def data_received(self, chunk):
        if self._refused:
            return
        self._size += len(chunk)
        if self._size > self.LIMIT:
            self._too_big = True
            self._chunks = []
        elif not self._too_big:
            self._chunks.append(chunk)

    def raw_body(self):
        if self._too_big:
            raise web.HTTPError(413, reason=f"body over {self.LIMIT} bytes")
        return b"".join(self._chunks)

    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")
        self.set_header("Cache-Control", "no-store")

    def write_error(self, status, **kw):
        # Tornado puts only a safe subset of the reason in the status line
        # ("Unknown" otherwise); the JSON body carries the full message.
        exc = (kw.get("exc_info") or (None, None, None))[1]
        self.finish({"error": getattr(exc, "reason", None) or self._reason, "status": status})

    def body_json(self):
        raw = self.raw_body()
        try:
            return strict_json(raw or b"{}")
        except (ValueError, UnicodeDecodeError) as e:
            raise web.HTTPError(400, reason=f"invalid JSON: {e}")

    async def user(self):
        if self._user is not None:
            return self._user
        if self.via_hub:
            return self.hub_user()
        h = self.request.headers.get("Authorization", "")
        m = re.match(r"(?i)^(bearer|token)\s+(\S+)$", h)
        if not m:
            raise web.HTTPError(401, reason="Authorization: Bearer <token> required (make one at /hub/token)")
        try:
            u = await self.gw.hub.whoami(m.group(2))
        except Exception:
            raise web.HTTPError(503, reason="cannot verify the token right now")
        if not u:
            raise web.HTTPError(401, reason="token rejected")
        if not u["serverless"]:
            raise web.HTTPError(403, reason=f"this token lacks the {INVOKE_SCOPE} scope; make one at /hub/token")
        return u

    def hub_user(self):
        """The hub port: the hub's pages call on behalf of a logged-in user.
        Authenticated by the hub<->gateway service token, which only the hub
        and the gateway hold; the NetworkPolicy also admits only the hub pod."""
        h = self.request.headers.get("Authorization", "")
        if not hmac.compare_digest(h.encode(), f"token {HUB_TOKEN}".encode()):
            raise web.HTTPError(401, reason="hub token required")
        name = self.request.headers.get("X-Zai-User", "")
        if not USER_RE.match(name):
            raise web.HTTPError(400, reason="bad X-Zai-User")
        return {"name": name, "admin": self.request.headers.get("X-Zai-Admin") == "1"}

    async def endpoint(self, ep_id):
        u = await self.user()
        if not EP_RE.match(ep_id):
            raise web.HTTPError(404, reason="no such endpoint")
        ep = self.gw.db.one("SELECT * FROM endpoints WHERE id=? AND deleted IS NULL", (ep_id,))
        # 404 rather than 403 for someone else's endpoint: ids are not to be probed.
        if not ep or (ep["owner"] != u["name"] and not u["admin"]):
            raise web.HTTPError(404, reason="no such endpoint")
        return u, ep

    def job(self, ep, job_id):
        j = self.gw.db.one("SELECT * FROM jobs WHERE id=? AND endpoint=?", (job_id, ep["id"])) \
            if JOB_RE.match(job_id) else None
        if not j:
            raise web.HTTPError(404, reason="no such job (results are kept for a limited time)")
        return j


class EndpointsHandler(Base):
    async def get(self):
        u = await self.user()
        if u["admin"] and self.get_argument("all", "") == "1":
            rows = self.gw.db.q("SELECT * FROM endpoints WHERE deleted IS NULL ORDER BY created")
        else:
            rows = self.gw.db.q("SELECT * FROM endpoints WHERE deleted IS NULL AND owner=? ORDER BY created",
                                (u["name"],))
        self.finish({"endpoints": [self.gw.endpoint_view(e) for e in rows]})

    async def post(self):
        u = await self.user()
        if not u["admin"]:
            raise web.HTTPError(403, reason="only administrators create endpoints for now")
        if not self.gw.catalog.enabled:
            raise web.HTTPError(403, reason="serverless is disabled in the catalog")
        try:
            e = self.gw.validate_endpoint(self.body_json())
        except ValueError as ex:
            raise web.HTTPError(400, reason=str(ex))
        if not await self.gw.k8s.home_pvc(e["owner"]):
            raise web.HTTPError(400, reason=f"{e['owner']} has no home volume yet: they must start a server once")
        ep_id = _rand(12)
        self.gw.db.x("INSERT INTO endpoints(id, owner, name, size, template, workdir, command, env, max_workers,"
                     " idle_timeout, exec_timeout, created, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (ep_id, e["owner"], e["name"], e["size"], e["template"], e["workdir"], e["command"], e["env"],
                      e["max_workers"], e["idle_timeout"], e["exec_timeout"], time.time(), u["name"]))
        log.info("endpoint %s created for %s by %s", ep_id, e["owner"], u["name"])
        self.set_status(201)
        self.finish(self.gw.endpoint_view(self.gw.db.one("SELECT * FROM endpoints WHERE id=?", (ep_id,))))


class EndpointHandler(Base):
    async def delete(self, ep_id):
        u, ep = await self.endpoint(ep_id)
        if not u["admin"]:
            raise web.HTTPError(403, reason="only administrators delete endpoints for now")
        self.gw.db.x("UPDATE endpoints SET deleted=? WHERE id=?", (time.time(), ep_id))
        for j in self.gw.db.q("SELECT id FROM jobs WHERE endpoint=? AND status IN ('IN_QUEUE','IN_PROGRESS')",
                              (ep_id,)):
            self.gw.finish_job(j["id"], "CANCELLED", error="endpoint deleted")
        for w in self.gw.db.q("SELECT id FROM workers WHERE endpoint=? AND state NOT IN ('stopping','gone')",
                              (ep_id,)):
            await self.gw.stop_worker(w["id"], "cancelled")
        self.finish({"deleted": ep_id})


class RunHandler(Base):
    async def post(self, ep_id, mode):
        _, ep = await self.endpoint(ep_id)
        job_id = self.gw.submit(ep, self.body_json(), sync=(mode == "runsync"))
        if mode == "run":
            return self.finish({"id": job_id, "status": "IN_QUEUE"})
        wait = min(RUNSYNC_WAIT, max(1, _int_arg(self, "wait", RUNSYNC_WAIT * 1000) // 1000))
        ev = self.gw._event(self.gw.job_events, job_id)
        try:
            await asyncio.wait_for(ev.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
        j = self.gw.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        self.finish(self.gw.job_view(j))


class StatusHandler(Base):
    async def get(self, ep_id, job_id):
        _, ep = await self.endpoint(ep_id)
        self.finish(self.gw.job_view(self.job(ep, job_id)))

    post = get


class StreamHandler(Base):
    async def get(self, ep_id, job_id):
        _, ep = await self.endpoint(ep_id)
        j = self.job(ep, job_id)
        rows = self.gw.db.q("SELECT seq, chunk FROM stream WHERE job=? AND read=0 ORDER BY seq", (job_id,))
        if rows:
            self.gw.db.x("UPDATE stream SET read=1 WHERE job=? AND seq<=?", (job_id, rows[-1]["seq"]))
        self.finish({"status": j["status"], "stream": [{"output": json.loads(r["chunk"])} for r in rows]})

    post = get


class CancelHandler(Base):
    async def post(self, ep_id, job_id):
        _, ep = await self.endpoint(ep_id)
        j = self.job(ep, job_id)
        if j["status"] not in TERMINAL:
            self.gw.finish_job(job_id, "CANCELLED")
            if j["status"] == "IN_PROGRESS" and j["worker"]:
                # Stopping the worker is the one cancellation every handler
                # honours, including ones that never look at the stop channel.
                await self.gw.stop_worker(j["worker"], "cancelled")
        self.finish(self.gw.job_view(self.gw.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))))


class PurgeHandler(Base):
    async def post(self, ep_id):
        _, ep = await self.endpoint(ep_id)
        rows = self.gw.db.q("SELECT id FROM jobs WHERE endpoint=? AND status='IN_QUEUE'", (ep["id"],))
        for r in rows:
            self.gw.finish_job(r["id"], "CANCELLED", error="queue purged")
        self.finish({"removed": len(rows), "status": "completed"})


class JobsHandler(Base):
    """Recent jobs, newest first: metadata only. Inputs and outputs stay
    behind /status, and finished jobs are purged after RESULT_TTL."""

    async def get(self, ep_id):
        _, ep = await self.endpoint(ep_id)
        rows = self.gw.db.q("SELECT id, status, created, started, finished, worker, error FROM jobs "
                            "WHERE endpoint=? ORDER BY created DESC LIMIT 50", (ep["id"],))
        jobs = []
        for j in rows:
            v = {"id": j["id"], "status": j["status"], "createdAt": int(j["created"] * 1000)}
            if j["started"]:
                v["delayTime"] = int((j["started"] - j["created"]) * 1000)
            if j["finished"]:
                v["finishedAt"] = int(j["finished"] * 1000)
                if j["started"]:
                    v["executionTime"] = int((j["finished"] - j["started"]) * 1000)
            if j["error"]:
                v["error"] = j["error"][:300]
            jobs.append(v)
        self.finish({"jobs": jobs})


class HealthHandler(Base):
    async def get(self, ep_id):
        _, ep = await self.endpoint(ep_id)
        counts = {r["status"]: r["n"] for r in self.gw.db.q(
            "SELECT status, COUNT(*) n FROM jobs WHERE endpoint=? GROUP BY status", (ep["id"],))}
        retried = self.gw.db.one("SELECT COUNT(*) FROM jobs WHERE endpoint=? AND retries>0", (ep["id"],))[0]
        ws = {r["state"]: r["n"] for r in self.gw.db.q(
            "SELECT state, COUNT(*) n FROM workers WHERE endpoint=? AND state!='gone' GROUP BY state", (ep["id"],))}
        self.finish({
            "jobs": {"completed": counts.get("COMPLETED", 0), "failed": counts.get("FAILED", 0)
                     + counts.get("TIMED_OUT", 0), "inProgress": counts.get("IN_PROGRESS", 0),
                     "inQueue": counts.get("IN_QUEUE", 0), "retried": retried},
            "workers": {"idle": ws.get("idle", 0), "running": ws.get("busy", 0),
                        "initializing": ws.get("starting", 0), "stopping": ws.get("stopping", 0)},
            "lastError": ep["last_error"] or None,
        })


class Healthz(web.RequestHandler):
    def get(self):
        self.finish("ok\n")


# ---------------------------------------------------------------------------
# HTTP: internal worker protocol (RunPod's, as runpod-python 1.12 speaks it)
# ---------------------------------------------------------------------------

class WorkerBase(Base):
    LIMIT = MAX_OUTPUT
    AUTH_FIRST = False      # workers authenticate with their own secret, per request

    def worker(self, wid):
        w = self.gw.db.one("SELECT * FROM workers WHERE id=?", (wid,)) if WORKER_RE.match(wid) else None
        key = self.request.headers.get("Authorization", "")
        if not w or not hmac.compare_digest(w["secret_hash"], _sha(key)):
            raise web.HTTPError(401, reason="unknown worker")
        if w["state"] in ("stopping", "gone"):
            raise web.HTTPError(410, reason="worker is stopping")
        return w


class JobTakeHandler(WorkerBase):
    async def get(self, kind, wid):
        w = self.worker(wid)
        limit = 1
        if kind == "job-take-batch":
            limit = max(1, min(16, _int_arg(self, "batch_size", 1)))
        deadline = time.time() + LONG_POLL
        while True:
            jobs = self.gw.take(w, limit)
            if jobs:
                return self.finish(compact(jobs if kind == "job-take-batch" else jobs[0]))
            remaining = deadline - time.time()
            if remaining <= 0 or self.request.connection.stream.closed():
                self.set_status(204)
                return self.finish()
            ev = self.gw._event(self.gw.queue_events, w["endpoint"])
            try:
                await asyncio.wait_for(ev.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass
            w = self.worker(wid)


class JobStopHandler(WorkerBase):
    async def get(self, wid):
        self.worker(wid)
        ev = self.gw._event(self.gw.stop_events, wid)
        try:
            await asyncio.wait_for(ev.wait(), timeout=LONG_POLL)
        except asyncio.TimeoutError:
            pass
        self.set_status(204)
        self.finish()


class JobDoneHandler(WorkerBase):
    async def post(self, kind, wid):
        w = self.worker(wid)
        job_id = self.get_argument("id", "")
        j = self.gw.db.one("SELECT * FROM jobs WHERE id=? AND worker=?", (job_id, wid)) \
            if JOB_RE.match(job_id) else None
        if not j:
            raise web.HTTPError(404, reason="job is not assigned to this worker")
        if self._too_big:
            if kind == "job-done" and j["status"] == "IN_PROGRESS":
                self.gw.finish_job(job_id, "FAILED", error=f"output over {MAX_OUTPUT} bytes")
                self.gw.db.x("UPDATE workers SET state='idle', last_active=? WHERE id=? AND state='busy'",
                             (time.time(), wid))
            raise web.HTTPError(413, reason="output too large")
        try:
            res = strict_json(self.raw_body())
        except (ValueError, UnicodeDecodeError) as e:
            raise web.HTTPError(400, reason=f"invalid JSON: {e}")
        if not isinstance(res, dict):
            raise web.HTTPError(400, reason="result must be an object")
        if j["status"] != "IN_PROGRESS":
            return self.finish({"ignored": True})     # cancelled or timed out meanwhile
        size = len(self.raw_body())
        if self.gw.stored_bytes(j["owner"]) + size > MAX_STORED:
            # One owner must not fill the disk every endpoint's queue lives on.
            self.gw.finish_job(job_id, "FAILED", error=f"over {MAX_STORED} bytes of results kept for this "
                                                        "account; fetch or let older results expire")
            self.gw.db.x("UPDATE workers SET state='idle', last_active=? WHERE id=? AND state='busy'",
                         (time.time(), wid))
            raise web.HTTPError(413, reason="account result storage full")
        if kind == "job-stream" or self.get_argument("isStream", "false") == "true":
            streamed = self.gw.db.one("SELECT COALESCE(SUM(LENGTH(chunk)),0) FROM stream WHERE job=?", (job_id,))[0]
            if streamed + size > MAX_STREAM:
                self.gw.finish_job(job_id, "FAILED", error=f"stream over {MAX_STREAM} bytes")
                self.gw.db.x("UPDATE workers SET state='idle', last_active=? WHERE id=? AND state='busy'",
                             (time.time(), wid))
                raise web.HTTPError(413, reason="stream too large")
            if "output" in res:
                seq = self.gw.db.one("SELECT COALESCE(MAX(seq),0)+1 FROM stream WHERE job=?", (job_id,))[0]
                self.gw.db.x("INSERT INTO stream(job, seq, chunk) VALUES (?,?,?)", (job_id, seq, compact(res["output"])))
            return self.finish({})
        if res.get("error"):
            err = res["error"] if isinstance(res["error"], str) else compact(res["error"])
            self.gw.finish_job(job_id, "FAILED", error=err[:65536])
        else:
            self.gw.finish_job(job_id, "COMPLETED", output=compact(res.get("output")))
        now = time.time()
        self.gw.db.x("UPDATE workers SET state='idle', last_active=? WHERE id=? AND state='busy'", (now, wid))
        if res.get("stopPod"):
            await self.gw.stop_worker(wid, "refresh_worker")
        self.finish({})


class PingHandler(WorkerBase):
    def get(self, wid):
        self.worker(wid)
        self.finish({})


def _api_routes(a):
    return [
        (r"/v2/endpoints", EndpointsHandler, a),
        (r"/v2/endpoints/([a-z0-9]+)", EndpointHandler, a),
        (r"/v2/([a-z0-9]+)/(run|runsync)", RunHandler, a),
        (r"/v2/([a-z0-9]+)/status/([0-9a-zA-Z-]+)", StatusHandler, a),
        (r"/v2/([a-z0-9]+)/stream/([0-9a-zA-Z-]+)", StreamHandler, a),
        (r"/v2/([a-z0-9]+)/cancel/([0-9a-zA-Z-]+)", CancelHandler, a),
        (r"/v2/([a-z0-9]+)/purge-queue", PurgeHandler, a),
        (r"/v2/([a-z0-9]+)/health", HealthHandler, a),
        (r"/v2/([a-z0-9]+)/jobs", JobsHandler, a),
    ]


def make_apps(gw):
    a = {"gw": gw}
    public = web.Application([(r"/healthz", Healthz)] + _api_routes(a))
    # The same API for the hub's Endpoints page, on its own port so that
    # neither the internet (public) nor workers (internal) can reach it.
    hub = web.Application([(r"/healthz", Healthz)] + _api_routes({"gw": gw, "via_hub": True}))
    internal = web.Application([
        (r"/healthz", Healthz),
        (r"/internal/(job-take|job-take-batch)/([a-z0-9-]+)", JobTakeHandler, a),
        (r"/internal/job-stop/([a-z0-9-]+)", JobStopHandler, a),
        (r"/internal/(job-done|job-stream)/([a-z0-9-]+)", JobDoneHandler, a),
        (r"/internal/ping/([a-z0-9-]+)", PingHandler, a),
    ])
    return public, internal, hub


async def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not HUB_TOKEN:
        raise SystemExit("HUB_TOKEN is not set")
    gw = Gateway()
    public, internal, hub = make_apps(gw)
    # The handlers enforce the real limits (and answer 413); these only stop
    # an unbounded upload from being read forever.
    HTTPServer(public, max_body_size=4 * MAX_INPUT).listen(int(_env("PUBLIC_PORT", 8080)))
    HTTPServer(internal, max_body_size=4 * MAX_OUTPUT).listen(int(_env("INTERNAL_PORT", 8081)))
    HTTPServer(hub, max_body_size=4 * MAX_INPUT).listen(int(_env("GW_HUB_PORT", 8082)))
    log.info("serverless gateway up: public :%s, internal :%s, hub :%s",
             _env("PUBLIC_PORT", 8080), _env("INTERNAL_PORT", 8081), _env("GW_HUB_PORT", 8082))
    await gw.run_loop()


if __name__ == "__main__":
    asyncio.run(main())
