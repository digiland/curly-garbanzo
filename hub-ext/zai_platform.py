"""ZAI platform layer for JupyterHub.

Turns the notebook hub into a pay-as-you-go GPU service:

- sizes and templates come from catalog.yaml, not from hand-written profiles
- prepaid credit: admins add money to an account, servers are charged by the
  second while running, a start needs credit and a running server is stopped
  when its owner's balance reaches zero
- GPU capacity is checked before a start, instead of leaving a pod Pending
- pages: /hub/account (everyone), /hub/billing and /hub/usage (admins)
- JSON API under /hub/api/zai/ for the `zai` CLI and future payment hooks

Loaded by extraConfig `03-zai-platform` in charts/jupyterhub-values.yaml from
the hub-zai ConfigMap (hub-ext/ in the repo). Everything is in one SQLite file
on the hub PVC, next to jupyterhub.sqlite, so it survives upgrades.

Money is stored as integer *micros* (millionths of the currency unit). A
$1.32/h slice costs ~367 micros a second; with cents, short sessions would
round to nothing.
"""

import csv
import asyncio
import inspect
import io
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import yaml
from jupyterhub import orm
from jupyterhub.apihandlers.base import APIHandler
from jupyterhub.handlers.base import BaseHandler
from kubespawner import KubeSpawner
from kubernetes_asyncio.client.models import V1EnvVar
from tornado import web
from tornado.httpclient import AsyncHTTPClient, HTTPRequest

import zai_2fa

DB_PATH = os.environ.get("ZAI_DB", "/srv/jupyterhub/usage.sqlite")
CATALOG_PATH = os.environ.get("ZAI_CATALOG", "/etc/jupyterhub/zai/catalog.yaml")
CLI_PATH = os.environ.get("ZAI_CLI", "/etc/jupyterhub/zai/zai")

MICROS = 1_000_000
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def to_micros(amount):
    """Currency amount (str/int/float, e.g. "12.50") -> integer micros."""
    s = str(amount).strip()
    # At most 9,999,999.999999: larger values overflow SQLite's integers.
    if not re.match(r"^-?\d{1,7}(\.\d{1,6})?$", s):
        raise ValueError(f"not an amount: {amount!r}")
    neg = s.startswith("-")
    whole, _, frac = s.lstrip("-").partition(".")
    v = int(whole) * MICROS + int((frac + "000000")[:6])
    return -v if neg else v


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

class Catalog:
    def __init__(self, data):
        cur = data.get("currency") or {}
        self.currency = str(cur.get("code", "USD"))
        self.symbol = str(cur.get("symbol", "$"))
        # Said after every price, e.g. "incl. VAT"; empty says nothing.
        self.price_note = str(cur.get("price_note") or "").strip()
        self.vat_percent = float(cur.get("vat_percent") or 0)

        b = data.get("billing") or {}
        self.enforce = bool(b.get("enforce", False))
        self.min_minutes = float(b.get("min_minutes_to_start", 10))
        self.admins_free = bool(b.get("admins_free", True))
        self.signup_credit = to_micros(b.get("signup_credit", 0))
        self.low_balance = to_micros(b.get("low_balance", 0))

        self.pools = {str(k): int(v) for k, v in (data.get("gpu_pools") or {}).items()}
        # What the start page calls each pool ("H200 slices"); optional.
        self.pool_labels = {str(k): str(v) for k, v in (data.get("gpu_pool_labels") or {}).items()}

        sv = data.get("serverless") or {}
        self.sv_enabled = bool(sv.get("enabled", False))
        # GPUs serverless workers may hold at once, across everyone. Keeps a
        # burst of jobs from locking notebook users out of the GPU.
        self.sv_max_gpus = int(sv.get("max_gpus", 2))
        self.sv_max_workers = int(sv.get("max_workers_per_endpoint", 3))

        self.templates = []
        for t in data.get("templates") or []:
            slug = t["slug"]
            if not SLUG_RE.match(slug):
                raise ValueError(f"template slug {slug!r} is not a-z0-9-")
            self.templates.append({
                "slug": slug,
                "name": t.get("name", slug),
                "description": t.get("description", ""),
                "image": t["image"],
                "gpu_required": bool(t.get("gpu_required", False)),
                "default_url": t.get("default_url"),
                "default": bool(t.get("default", False)),
            })
        if not self.templates:
            raise ValueError("catalog has no templates")
        if not any(t["default"] for t in self.templates):
            self.templates[0]["default"] = True

        self.sizes = []
        for s in data.get("sizes") or []:
            slug = s["slug"]
            if not SLUG_RE.match(slug):
                raise ValueError(f"size slug {slug!r} is not a-z0-9-")
            gpu = s.get("gpu") or None
            if gpu:
                # `pool` names the gpu_pools entry this size draws from. It
                # defaults to the resource name; set it when two kinds of GPU
                # share one resource name (on a cluster, a MIG slice and a whole
                # GPU can both be plain nvidia.com/gpu on different node pools).
                gpu = {"resource": str(gpu["resource"]), "count": int(gpu.get("count", 1)),
                       "vram_gb": gpu.get("vram_gb"), "pool": str(gpu.get("pool", gpu["resource"]))}
                if gpu["pool"] not in self.pools:
                    raise ValueError(f"size {slug}: pool {gpu['pool']} is not in gpu_pools")
            self.sizes.append({
                "slug": slug,
                "name": s.get("name", slug),
                "description": s.get("description", ""),
                "gpu": gpu,
                "cpu": s.get("cpu", 2),
                "cpu_guarantee": s.get("cpu_guarantee", 0.5),
                "memory": str(s.get("memory", "8G")),
                "memory_guarantee": str(s.get("memory_guarantee", "1G")),
                "node_selector": s.get("node_selector") or None,
                "runtime_class": s.get("runtime_class") or None,
                "rate": to_micros(s.get("price_per_hour", 0)),   # micros per hour
                "default": bool(s.get("default", False)),
            })
        if not self.sizes:
            raise ValueError("catalog has no sizes")
        if not any(z["default"] for z in self.sizes):
            self.sizes[0]["default"] = True

    @classmethod
    def load(cls, path=CATALOG_PATH):
        with open(path) as f:
            return cls(yaml.safe_load(f) or {})

    def size(self, slug):
        for z in self.sizes:
            if z["slug"] == slug:
                return z
        return None

    @property
    def default_size(self):
        return next(z for z in self.sizes if z["default"])

    def templates_for(self, size):
        return [t for t in self.templates if size["gpu"] or not t["gpu_required"]]

    def template(self, slug):
        return next((t for t in self.templates if t["slug"] == slug), None)

    def money(self, micros):
        sign = "-" if micros < 0 else ""
        return f"{sign}{self.symbol}{abs(micros) / MICROS:,.2f}"

    def rate_str(self, micros_per_hour):
        v = micros_per_hour / MICROS
        s = f"{self.symbol}{v:,.2f}/h" if v >= 0.1 else f"{self.symbol}{v:,.3f}/h"
        return f"{s} {self.price_note}" if self.price_note else s

    def profile_list(self):
        """KubeSpawner profiles: one per size, with the templates as an option."""
        out = []
        for z in self.sizes:
            override = {
                "cpu_limit": z["cpu"],
                "cpu_guarantee": z["cpu_guarantee"],
                "mem_limit": z["memory"],
                "mem_guarantee": z["memory_guarantee"],
                "extra_resource_limits": (
                    {z["gpu"]["resource"]: str(z["gpu"]["count"])} if z["gpu"] else {}),
            }
            if z["node_selector"]:
                override["node_selector"] = z["node_selector"]
            if z["runtime_class"]:
                override["extra_pod_config"] = {"runtimeClassName": z["runtime_class"]}
            tpls = self.templates_for(z)
            if not tpls:
                raise ValueError(f"size {z['slug']} has no template it can run")
            has_default = any(t["default"] for t in tpls)
            choices = {}
            for i, t in enumerate(tpls):
                o = {"image": t["image"]}
                if t["default_url"]:
                    o["default_url"] = t["default_url"]
                choices[t["slug"]] = {
                    "display_name": t["name"],
                    "default": t["default"] if has_default else i == 0,
                    "kubespawner_override": o,
                }
            out.append({
                "display_name": z["name"],
                "slug": z["slug"],
                "description": z["description"],
                "default": z["default"],
                "kubespawner_override": override,
                "profile_options": {"template": {"display_name": "Template", "choices": choices}},
            })
        return out


CATALOG = None   # set by configure()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

@contextmanager
def _db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        with con:              # one transaction, committed on success
            yield con
    finally:
        con.close()


def init_db():
    with _db() as con:
        # `sessions` predates billing (03-usage-accounting); its first columns
        # keep their meaning so old rows stay valid. A session with rate 0 --
        # every pre-billing row -- is never charged.
        con.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id       INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                server   TEXT NOT NULL DEFAULT '',
                profile  TEXT NOT NULL DEFAULT '',
                gpu      INTEGER NOT NULL DEFAULT 0,
                started  REAL NOT NULL,
                stopped  REAL
            )""")
        have = {r["name"] for r in con.execute("PRAGMA table_info(sessions)")}
        for col, ddl in [
            ("kind",     "TEXT NOT NULL DEFAULT 'notebook'"),  # notebook | worker (serverless, later)
            ("size",     "TEXT NOT NULL DEFAULT ''"),
            ("template", "TEXT NOT NULL DEFAULT ''"),
            ("resource", "TEXT NOT NULL DEFAULT ''"),          # gpu_pools key held, '' = none
            ("gpus",     "INTEGER NOT NULL DEFAULT 0"),
            ("rate",     "INTEGER NOT NULL DEFAULT 0"),        # micros/hour, snapshotted at start
            ("charged",  "INTEGER NOT NULL DEFAULT 0"),        # 1 once its ledger row exists
            ("endpoint", "TEXT NOT NULL DEFAULT ''"),          # workers: the serverless endpoint id
        ]:
            if col not in have:
                con.execute(f"ALTER TABLE sessions ADD COLUMN {col} {ddl}")
        con.execute("CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(username)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_sessions_open ON sessions(stopped)")
        # Append-only money ledger. Balance = SUM(micros) minus the running cost
        # of sessions not yet charged. Usage rows are negative.
        con.execute("""
            CREATE TABLE IF NOT EXISTS ledger (
                id         INTEGER PRIMARY KEY,
                username   TEXT NOT NULL,
                micros     INTEGER NOT NULL,
                kind       TEXT NOT NULL,      -- topup | adjust | usage | signup
                note       TEXT NOT NULL DEFAULT '',
                actor      TEXT NOT NULL DEFAULT '',
                session_id INTEGER,
                ts         REAL NOT NULL
            )""")
        con.execute("CREATE INDEX IF NOT EXISTS ix_ledger_user ON ledger(username, ts)")


def session_cost(rate, started, end):
    return int(rate * max(0.0, end - started) / 3600)


def _ensure_account(con, username):
    """Grant signup credit the first time billing sees an account."""
    if CATALOG.signup_credit <= 0:
        return
    seen = con.execute("SELECT 1 FROM ledger WHERE username=? AND kind='signup'", (username,)).fetchone()
    if not seen:
        con.execute("INSERT INTO ledger(username, micros, kind, note, actor, ts) VALUES (?,?,?,?,?,?)",
                    (username, CATALOG.signup_credit, "signup", "Welcome credit", "system", time.time()))


def account(con, username, now=None):
    """Balance, plus what is currently accruing, for one user."""
    now = now or time.time()
    _ensure_account(con, username)
    paid = con.execute("SELECT COALESCE(SUM(micros),0) FROM ledger WHERE username=?",
                       (username,)).fetchone()[0]
    accruing, running = 0, []
    for r in con.execute("SELECT * FROM sessions WHERE username=? AND charged=0 AND rate>0", (username,)):
        end = r["stopped"] if r["stopped"] is not None else now
        cost = session_cost(r["rate"], r["started"], end)
        accruing += cost
        if r["stopped"] is None:
            running.append({"id": r["id"], "size": r["size"], "template": r["template"],
                            "rate": r["rate"], "started": r["started"], "cost": cost})
    return {"balance": paid - accruing, "accruing": accruing, "running": running}


def add_credit(username, micros, kind, note, actor):
    if kind not in ("topup", "adjust"):
        raise ValueError("kind must be topup or adjust")
    with _db() as con:
        con.execute("INSERT INTO ledger(username, micros, kind, note, actor, ts) VALUES (?,?,?,?,?,?)",
                    (username, micros, kind, note[:200], actor, time.time()))
        return account(con, username)["balance"]


def gpus_in_use(con, pool):
    """GPUs from `pool` held right now, by everything billing knows about.

    The one place capacity is counted: serverless workers will record sessions
    here too, so notebooks and workers share one view of the GPU. Rows from
    before billing have no resource and count as one plain `nvidia.com/gpu`.
    """
    n = 0
    for r in con.execute("SELECT resource, gpus, gpu FROM sessions WHERE stopped IS NULL"):
        res, cnt = r["resource"], r["gpus"]
        if not res and r["gpu"]:
            res, cnt = "nvidia.com/gpu", 1
        if res == pool:
            n += cnt
    return n + sum(c for (res, c) in _starting.values() if res == pool)


def worker_gpus_in_use(con):
    return con.execute("SELECT COALESCE(SUM(gpus),0) FROM sessions WHERE kind='worker' AND stopped IS NULL"
                       ).fetchone()[0]


# (user, server) -> (resource, count) for starts in flight, so two people
# racing for the last slice cannot both pass the capacity check.
_starting = {}


def availability(con):
    return {res: max(0, cap - gpus_in_use(con, res)) for res, cap in CATALOG.pools.items()}


def _endpoint_label(body):
    """What a worker session shows as: its endpoint's name, else its id."""
    name = " ".join(str(body.get("endpoint_name", "")).split())[:48]
    return name or str(body.get("endpoint", ""))[:64]


def _dur(secs):
    m = int(secs // 60)
    if m < 1:
        return "<1m"
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def _close_sessions(con, username, server, stopped, now):
    """Close open sessions and post their charge, atomically."""
    rows = con.execute("SELECT * FROM sessions WHERE username=? AND server=? AND stopped IS NULL",
                       (username, server)).fetchall()
    for r in rows:
        end = stopped if stopped is not None else r["started"]
        con.execute("UPDATE sessions SET stopped=?, charged=1 WHERE id=?", (end, r["id"]))
        cost = session_cost(r["rate"], r["started"], end)
        if cost > 0:
            size = CATALOG.size(r["size"])
            label = size["name"] if size else (r["size"] or r["profile"])
            if r["kind"] == "worker":
                label = f"Serverless {r['template']} · {label}"
            con.execute(
                "INSERT INTO ledger(username, micros, kind, note, actor, session_id, ts) VALUES (?,?,?,?,?,?,?)",
                (username, -cost, "usage", f"{label} · {_dur(end - r['started'])}", "system", r["id"], now))


# ---------------------------------------------------------------------------
# Spawner
# ---------------------------------------------------------------------------

class ZaiSpawner(KubeSpawner):
    """KubeSpawner that checks credit and capacity, and meters each session."""

    def _zai_choice(self):
        opts = self.user_options or {}
        size = CATALOG.size(opts.get("profile") or "") or CATALOG.default_size
        allowed = CATALOG.templates_for(size)
        tpl = next((t for t in allowed if t["slug"] == opts.get("template")), None)
        if opts.get("template") and tpl is None:
            raise web.HTTPError(400, f"Template {opts['template']!r} cannot run on {size['name']}.")
        tpl = tpl or next((t for t in allowed if t["default"]), allowed[0])
        rate = 0 if (self.user.admin and CATALOG.admins_free) else size["rate"]
        return size, tpl, rate

    def options_from_query(self, query_data):
        # JupyterHub starts a server straight from /hub/spawn?profile=...; that
        # is a GET, which has no XSRF check, so any site could start a billed
        # GPU server for a logged-in visitor. Starts come from the form or API.
        raise web.HTTPError(400, "Servers are started from this page, not from a link. Choose a size below.")

    async def get_pod_manifest(self):
        """A pod without a GPU must not see one. The nvidia runtime attaches
        whatever NVIDIA_VISIBLE_DEVICES names, the CUDA images set it to
        "all", and the device plugin only overrides it for pods it gave a GPU:
        a CPU-only server would otherwise see every MIG slice on the node."""
        pod = await super().get_pod_manifest()
        limits = {}
        for ctr in pod.spec.containers:
            limits.update((ctr.resources.limits or {}) if ctr.resources else {})
        if not any(k.startswith("nvidia.com/") for k in limits):
            pod.spec.runtime_class_name = None
            for ctr in pod.spec.containers + (pod.spec.init_containers or []):
                env = [e for e in (ctr.env or []) if e.name != "NVIDIA_VISIBLE_DEVICES"]
                env.append(V1EnvVar(name="NVIDIA_VISIBLE_DEVICES", value="void"))
                ctr.env = env
        return pod

    async def start(self):
        size, tpl, rate = self._zai_choice()
        key = (self.user.name, self.name)
        now = time.time()
        with _db() as con:
            # A session left open by an unclean hub stop is closed uncharged
            # here rather than double-counted (its true end is unknown).
            _close_sessions(con, self.user.name, self.name, None, now)
            if CATALOG.enforce and rate > 0:
                bal = account(con, self.user.name, now)["balance"]
                need = int(rate * CATALOG.min_minutes / 60)
                if bal < need:
                    raise web.HTTPError(402, (
                        f"Not enough credit to start {size['name']}. Your balance is "
                        f"{CATALOG.money(bal)}; starting needs {CATALOG.money(need)} "
                        f"({CATALOG.min_minutes:g} minutes at {CATALOG.rate_str(rate)}). "
                        "Request more credit on your Account page."))
            if size["gpu"]:
                res, cnt = size["gpu"]["pool"], size["gpu"]["count"]
                cap = CATALOG.pools[res]
                used = gpus_in_use(con, res)
                if used + cnt > cap:
                    raise web.HTTPError(503, (
                        f"All {cap} GPUs for {size['name']} are in use. Try another size, "
                        "CPU only, or again in a few minutes."))
                _starting[key] = (res, cnt)
        try:
            result = await super().start()
        finally:
            _starting.pop(key, None)
        # Only a successful start counts: a failed spawn holds no slice.
        try:
            with _db() as con:
                con.execute(
                    "INSERT INTO sessions(username, server, profile, gpu, started, kind, size, template,"
                    " resource, gpus, rate) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (self.user.name, self.name, size["name"], int(bool(size["gpu"])), time.time(),
                     "notebook", size["slug"], tpl["slug"],
                     size["gpu"]["pool"] if size["gpu"] else "",
                     size["gpu"]["count"] if size["gpu"] else 0, rate))
        except Exception:
            self.log.exception("billing: could not record start for %s", self.user.name)
        return result

    async def poll(self):
        status = await super().poll()
        if status is not None or not CATALOG.enforce:
            return status
        # Out of credit -> delete the pod, then report the server as stopped.
        # The hub's poll callback then does the rest (remove the route, mark
        # it stopped, post-stop hook, which posts the charge). The pod must be
        # deleted HERE: User.stop() only calls spawner.stop() when poll() says
        # "running", so returning a status alone would leave the pod -- and
        # its GPU -- running unbilled with the hub believing it gone. Polling
        # covers every running server, including ones re-adopted after a hub
        # restart.
        try:
            with _db() as con:
                a = account(con, self.user.name)
                billable = any(r["rate"] > 0 for r in a["running"])
            if billable and a["balance"] <= 0:
                self.log.warning("billing: %s is out of credit (%s); stopping their server",
                                 self.user.name, CATALOG.money(a["balance"]))
                await self.stop()
                return 0
        except Exception:
            self.log.exception("billing: credit check failed for %s", self.user.name)
        return None

    async def run_post_stop_hook(self):
        # User.stop() runs this in a `finally`, so it fires for every way a
        # server ends: user stop, idle cull, out of credit, pod crash, and pods
        # found dead after a hub restart (then "now" over-counts by the downtime).
        try:
            now = time.time()
            with _db() as con:
                _close_sessions(con, self.user.name, self.name, now, now)
        except Exception:
            self.log.exception("billing: could not record stop for %s", self.user.name)
        # Synchronous in JupyterHub 5.5 (returns the hook's result, usually
        # None); awaiting it unconditionally raised on every stop.
        result = super().run_post_stop_hook()
        if inspect.isawaitable(result):
            result = await result
        return result


def options_form(spawner):
    """The size + template picker on /hub/spawn, with prices and free slices.

    Only markup: the form still sends `profile` (a size slug) and `template`,
    which options_from_form() and ZaiSpawner check. templates/spawn.html holds
    the summary bar that the script below fills in."""
    from html import escape as e
    user = spawner.user
    with _db() as con:
        acct = account(con, user.name)
        free = availability(con)
    admin_free = user.admin and CATALOG.admins_free
    note = f" {e(CATALOG.price_note)}" if CATALOG.price_note else ""
    rows = []
    for z in CATALOG.sizes:
        if z["gpu"]:
            n = free.get(z["gpu"]["pool"], 0) // max(1, z["gpu"]["count"])
            avail = f"{n} free" if n else "Full"
            full = n == 0
            vram = f"{z['gpu'].get('vram_gb') or '?'} GB"
        else:
            avail, full, vram = "Available", False, "None"
        v = z["rate"] / MICROS
        if admin_free:
            big, small = "Free", "for administrators"
        else:
            big = f"{CATALOG.symbol}{v:,.2f}" if v >= 0.01 else f"{CATALOG.symbol}{v:,.3f}"
            small = f"/ hour{note}"
        ram = re.sub(r"^([0-9.]+)([KMGT])i?$", r"\1 \2B", str(z["memory"]))     # "12G" -> "12 GB"
        tpls = " ".join(t["slug"] for t in CATALOG.templates_for(z))
        tag = '<span class="z-pill is-gold">Recommended</span>' if z["default"] else ""
        rows.append(f"""
      <label class="z-size{' is-full' if full else ''}">
        <span class="z-size-top">
          <span class="z-size-name"><input type="radio" name="profile" value="{e(z['slug'])}" data-templates="{e(tpls)}"
                 data-name="{e(z['name'])}" data-rate="{0 if admin_free else z['rate']}" data-price="{e(CATALOG.rate_str(z['rate']))}"
                 {'checked' if z['default'] and not full else ''} {'disabled' if full else ''}>{e(z['name'])}</span>
          {tag}
        </span>
        <span class="z-size-price"><b>{e(big)}</b><span>{small}</span></span>
        <span class="z-size-spec">
          <span>VRAM<b>{vram}</b></span><span>RAM<b>{e(ram)}</b></span><span>CPU<b>{z['cpu']} cores</b></span>
        </span>
        <span class="z-size-desc">{e(z['description'])}</span>
        <span class="z-pill{' is-bad' if full else ' is-ok'}">{avail}</span>
      </label>""")
    tiles = "".join(
        f"""
      <label class="z-tpl"><input type="radio" name="template" value="{e(t['slug'])}" data-name="{e(t['name'])}"
             {'checked' if t['default'] else ''}><span><b>{e(t['name'])}</b><small>{e(t['description'])}</small></span></label>"""
        for t in CATALOG.templates)
    slots = ""
    for res, cap in CATALOG.pools.items():
        left = free.get(res, 0)
        bar = "".join('<i class="on"></i>' if i < cap - left else "<i></i>" for i in range(cap))
        label = e(CATALOG.pool_labels.get(res, "GPU slices"))
        slots += (f'<div class="z-slots"><div class="z-slots-head"><span class="z-label">{label} free</span>'
                  f'<b>{left} of {cap}</b></div><div class="z-slots-bar" aria-hidden="true">{bar}</div></div>')
    bal = acct["balance"]
    if admin_free:
        head = '<p class="z-balance">Administrator: your servers are not charged.</p>'
    else:
        low = bal <= CATALOG.low_balance
        head = (f'<p class="z-balance{" is-low" if low else ""}">Balance <strong>{CATALOG.money(bal)}</strong>'
                f' · charged by the second while your server runs'
                f'{" · <a href=account#credit>request credit</a>" if low else ""}</p>')
    return f"""
<div class="z-spawn" data-balance="{bal}" data-money="{e(CATALOG.money(bal))}" data-free="{1 if admin_free else 0}">
  <div class="z-spawn-top">{head}{slots}</div>
  <fieldset class="z-sizes"><legend>Size</legend>{''.join(rows)}</fieldset>
  <fieldset class="z-tpls"><legend>Template</legend>{tiles}</fieldset>
</div>
<script>
(function () {{
  var box = document.querySelector('.z-spawn'), out = document.getElementById('z-summary');
  var tpls = document.querySelectorAll('input[name=template]');
  function sync() {{
    var r = document.querySelector('input[name=profile]:checked');
    var ok = r ? r.dataset.templates.split(' ') : [];
    tpls.forEach(function (t) {{ t.disabled = ok.indexOf(t.value) < 0; }});
    var t = document.querySelector('input[name=template]:checked');
    if (!t || t.disabled) {{
      t = Array.prototype.find.call(tpls, function (x) {{ return !x.disabled; }});
      if (t) t.checked = true;
    }}
    if (!out) return;
    if (!r) {{ out.textContent = 'Choose a size.'; return; }}
    var text = r.dataset.name + (t ? ' with ' + t.dataset.name : '');
    var rate = Number(r.dataset.rate), bal = Number(box.dataset.balance);
    if (box.dataset.free === '1') text += ' · not charged';
    else {{
      text += ' · ' + r.dataset.price;
      if (rate > 0 && bal > 0) {{
        var h = bal / rate;
        text += ' · your ' + box.dataset.money + ' covers about ' + (h < 10 ? h.toFixed(1) : Math.round(h)) + ' hours';
      }}
    }}
    out.textContent = text;
  }}
  document.querySelectorAll('input[name=profile], input[name=template]').forEach(function (x) {{ x.addEventListener('change', sync); }});
  // /hub/spawn#<size> (the size cards on Home) picks that size, if it is free.
  var want = document.querySelector('input[name=profile][value="' + location.hash.slice(1).replace(/[^a-z0-9-]/gi, '') + '"]');
  if (location.hash && want && !want.disabled) want.checked = true;
  sync();
}})();
</script>"""


def options_from_form(formdata):
    size = (formdata.get("profile") or [""])[0]
    tpl = (formdata.get("template") or [""])[0]
    if not CATALOG.size(size):
        raise ValueError("Pick a size.")
    out = {"profile": size}
    if tpl:
        out["template"] = tpl
    return out


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def _when(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %Y %H:%M")


def _size_name(slug):
    z = CATALOG.size(slug)
    return z["name"] if z else slug


# ---------------------------------------------------------------------------
# Serverless endpoints page. The data lives in the gateway; the hub asks for
# it on the logged-in user's behalf, over the gateway's hub-only port.
# ---------------------------------------------------------------------------

GATEWAY_URL = os.environ.get("ZAI_GATEWAY_URL", "http://serverless-gateway-internal:8082").rstrip("/")
GATEWAY_TOKEN = os.environ.get("ZAI_GATEWAY_TOKEN", "")
ENV_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class GatewayError(Exception):
    pass


async def gateway(user, method, path, body=None):
    """Call the gateway's API as `user`. The gateway applies its own checks
    (owner or admin) exactly as for a customer's token."""
    if not GATEWAY_TOKEN:
        raise GatewayError("Serverless is not set up on this hub.")
    req = HTTPRequest(
        GATEWAY_URL + path, method=method, request_timeout=15,
        body=json.dumps(body) if body is not None else (b"" if method == "POST" else None),
        headers={"Authorization": f"token {GATEWAY_TOKEN}", "X-Zai-User": user.name,
                 "X-Zai-Admin": "1" if user.admin else "0", "Content-Type": "application/json"})
    try:
        r = await AsyncHTTPClient().fetch(req, raise_error=False)
    except Exception:
        raise GatewayError("The serverless service is not reachable right now. Try again in a minute.")
    try:
        data = json.loads(r.body or b"{}")
    except ValueError:
        data = {}
    if r.code == 599:
        raise GatewayError("The serverless service is not reachable right now. Try again in a minute.")
    if r.code >= 400:
        raise GatewayError(str(data.get("error") or f"error {r.code}"))
    return data


def _endpoint_spend(con, ids, since, now):
    """Charged plus accruing, per endpoint id, since `since`."""
    out = {i: 0 for i in ids}
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    for r in con.execute(
            f"SELECT s.endpoint, COALESCE(-SUM(l.micros),0) AS m FROM ledger l JOIN sessions s ON s.id=l.session_id "
            f"WHERE s.kind='worker' AND l.kind='usage' AND l.ts>=? AND s.endpoint IN ({marks}) GROUP BY s.endpoint",
            (since, *ids)):
        out[r["endpoint"]] += r["m"]
    for r in con.execute(f"SELECT endpoint, rate, started FROM sessions WHERE kind='worker' AND charged=0 "
                         f"AND stopped IS NULL AND endpoint IN ({marks})", ids):
        out[r["endpoint"]] += session_cost(r["rate"], max(r["started"], since), now)
    return out


class EndpointsPage(BaseHandler):
    """GET/POST /hub/endpoints -- serverless endpoints: how to call them, their
    queue, workers and recent jobs. Owners send test jobs and cancel; admins
    see everyone's and create and delete."""

    _form = {}      # a create form that failed, to refill it

    @web.authenticated
    async def get(self, message="", error=""):
        user = self.current_user
        endpoints, unavailable = [], ""
        try:
            listed = await gateway(user, "GET", "/v2/endpoints" + ("?all=1" if user.admin else ""))
            eps = listed.get("endpoints", [])
            details = await asyncio.gather(*[
                asyncio.gather(gateway(user, "GET", f"/v2/{e['id']}/health"),
                               gateway(user, "GET", f"/v2/{e['id']}/jobs")) for e in eps])
            for e, (health, jobs) in zip(eps, details):
                e["health"], e["jobs"] = health, jobs.get("jobs", [])
                endpoints.append(e)
        except GatewayError as ex:
            unavailable = str(ex)
        now = time.time()
        month = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        with _db() as con:
            spend = _endpoint_spend(con, [e["id"] for e in endpoints], month, now)
        users = sorted(u.name for u in self.db.query(orm.User).all()) if user.admin else []
        html = await self.render_template(
            "endpoints.html", endpoints=endpoints, unavailable=unavailable, spend=spend,
            message=message, error=error, catalog=CATALOG, money=CATALOG.money, rate_str=CATALOG.rate_str,
            size_name=_size_name, public_host=self.request.host, users=users, now_ms=int(now * 1000),
            form=self._form)
        self.finish(html)

    @web.authenticated
    async def post(self):
        user = self.current_user
        action = self.get_body_argument("action", "")
        ep = self.get_body_argument("endpoint", "")
        if ep and not re.fullmatch(r"[a-z0-9]{12}", ep):
            raise web.HTTPError(400, "bad endpoint id")
        try:
            if action == "run":
                raw = self.get_body_argument("input", "").strip() or "{}"
                try:
                    payload = json.loads(raw)
                except ValueError as ex:
                    raise GatewayError(f"The input is not valid JSON: {ex}")
                r = await gateway(user, "POST", f"/v2/{ep}/run", {"input": payload})
                msg = f"Job {r.get('id', '')[:8]} queued. It appears under Recent jobs."
            elif action == "cancel":
                job = self.get_body_argument("job", "")
                if not re.fullmatch(r"[0-9a-f-]{36}", job):
                    raise web.HTTPError(400, "bad job id")
                r = await gateway(user, "POST", f"/v2/{ep}/cancel/{job}")
                msg = f"Job {job[:8]}: {r.get('status', '').lower().replace('_', ' ')}."
            elif action == "purge":
                r = await gateway(user, "POST", f"/v2/{ep}/purge-queue")
                msg = f"Removed {r.get('removed', 0)} queued job(s)."
            elif action == "create":
                if not user.admin:
                    raise web.HTTPError(403, "Administrators only")
                self._form = {k: self.get_body_argument(k, "") for k in (
                    "owner", "name", "size", "template", "workdir", "command",
                    "max_workers", "idle_timeout", "execution_timeout", "env")}
                r = await gateway(user, "POST", "/v2/endpoints", self._create_body(self._form))
                self._form = {}
                msg = f"Created {r['name']} ({r['id']}) for {r['owner']}."
            elif action == "delete":
                if not user.admin:
                    raise web.HTTPError(403, "Administrators only")
                await gateway(user, "DELETE", f"/v2/endpoints/{ep}")
                msg = f"Deleted endpoint {ep}. Its queued and running jobs were cancelled."
            else:
                raise web.HTTPError(400, "unknown action")
        except GatewayError as ex:
            return await self.get(error=str(ex))
        self.log.info("endpoints: %s %s %s", user.name, action, ep)
        await self.get(message=msg)

    @staticmethod
    def _create_body(f):
        def num(key):
            try:
                return int(f[key])
            except ValueError:
                raise GatewayError(f"{key.replace('_', ' ')}: a whole number")
        env = {}
        for n, line in enumerate(f["env"].splitlines(), 1):
            if not line.strip():
                continue
            m = ENV_LINE.match(line.strip())
            if not m:
                raise GatewayError(f"Environment line {n}: write NAME=value")
            env[m.group(1)] = m.group(2)
        return {"owner": f["owner"], "name": f["name"].strip(), "size": f["size"], "template": f["template"],
                "workdir": f["workdir"].strip().strip("/").removeprefix("~/"),
                "command": f["command"].strip() or "python handler.py", "env": env,
                "max_workers": num("max_workers"), "idle_timeout": num("idle_timeout"),
                "execution_timeout": num("execution_timeout")}


class AccountHandler(BaseHandler):
    """GET /hub/account -- balance, prices, history, how to connect."""

    @web.authenticated
    async def get(self):
        user = self.current_user
        with _db() as con:
            acct = account(con, user.name)
            free = availability(con)
            history = [dict(r) for r in con.execute(
                "SELECT * FROM ledger WHERE username=? ORDER BY ts DESC LIMIT 100", (user.name,))]
        html = await self.render_template(
            "account.html", acct=acct, history=history, catalog=CATALOG, free=free,
            money=CATALOG.money, rate_str=CATALOG.rate_str, when=_when, dur=_dur,
            size_name=_size_name, admin_free=user.admin and CATALOG.admins_free,
            public_host=self.request.host, now=time.time(), sync=False,
            two_factor=zai_2fa.enabled(self.db, user.name))
        self.finish(html)


class BillingHandler(BaseHandler):
    """GET/POST /hub/billing -- admin: balances, top-ups, the ledger."""

    def _admin(self):
        if not self.current_user.admin:
            raise web.HTTPError(403, "Administrators only")

    @web.authenticated
    async def get(self, message="", error=""):
        self._admin()
        only = self.get_argument("user", "")
        now = time.time()
        month = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        names = sorted(u.name for u in self.db.query(orm.User))
        with _db() as con:
            people = []
            for n in names:
                a = account(con, n, now)
                spent = -con.execute(
                    "SELECT COALESCE(SUM(micros),0) FROM ledger WHERE username=? AND kind='usage' AND ts>=?",
                    (n, month)).fetchone()[0]
                topped = con.execute(
                    "SELECT COALESCE(SUM(micros),0) FROM ledger WHERE username=? AND kind='topup' AND ts>=?",
                    (n, month)).fetchone()[0]
                people.append({"name": n, "balance": a["balance"], "running": a["running"],
                               "spent": spent + a["accruing"], "topped": topped})
            q = "SELECT * FROM ledger" + (" WHERE username=?" if only else "") + " ORDER BY ts DESC LIMIT 200"
            ledger = [dict(r) for r in con.execute(q, (only,) if only else ())]
            totals = con.execute(
                "SELECT COALESCE(SUM(CASE WHEN kind='topup' THEN micros END),0),"
                "       COALESCE(-SUM(CASE WHEN kind='usage' THEN micros END),0) FROM ledger WHERE ts>=?",
                (month,)).fetchone()
        if self.get_argument("format", "") == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["time_utc", "username", "kind", "amount", "currency", "note", "actor"])
            for r in reversed(ledger):
                w.writerow([datetime.fromtimestamp(r["ts"], timezone.utc).isoformat(timespec="seconds"),
                            r["username"], r["kind"], f"{r['micros'] / MICROS:.6f}", CATALOG.currency,
                            r["note"], r["actor"]])
            self.set_header("Content-Type", "text/csv; charset=utf-8")
            self.set_header("Content-Disposition", 'attachment; filename="ledger.csv"')
            return self.finish(buf.getvalue())
        html = await self.render_template(
            "billing.html", people=people, ledger=ledger, only=only, catalog=CATALOG,
            month_topups=totals[0], month_usage=totals[1], money=CATALOG.money, when=_when,
            message=message, error=error, sync=False)
        self.finish(html)

    @web.authenticated
    async def post(self):
        self._admin()
        username = self.get_body_argument("username", "").strip()
        kind = self.get_body_argument("kind", "topup")
        note = self.get_body_argument("note", "").strip()
        try:
            if not self.db.query(orm.User).filter_by(name=username).first():
                raise ValueError(f"No user {username!r}")
            micros = to_micros(self.get_body_argument("amount", ""))
            if kind == "topup" and micros <= 0:
                raise ValueError("A top-up must be positive; use an adjustment to take credit away")
            if micros == 0:
                raise ValueError("Amount is zero")
            bal = add_credit(username, micros, kind, note, self.current_user.name)
        except ValueError as ex:
            return await self.get(error=str(ex))
        self.log.info("billing: %s %s %s for %s (%s)", self.current_user.name, kind,
                      CATALOG.money(micros), username, note)
        await self.get(message=f"{CATALOG.money(micros)} added to {username}. New balance {CATALOG.money(bal)}.")


class UsageHandler(BaseHandler):
    """GET /hub/usage -- admin-only hours per user, HTML or CSV."""

    PERIODS = {"month": "This month", "30d": "Last 30 days", "all": "All time"}

    @web.authenticated
    async def get(self):
        if not self.current_user.admin:
            raise web.HTTPError(403, "Administrators only")
        period = self.get_argument("period", "month")
        if period not in self.PERIODS:
            period = "month"
        now = time.time()
        if period == "month":
            since = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        elif period == "30d":
            since = now - 30 * 86400
        else:
            since = 0.0

        with _db() as con:
            rows = con.execute(
                "SELECT username, server, profile, size, gpu, kind, started, stopped FROM sessions "
                "WHERE stopped IS NULL OR stopped >= ? ORDER BY started DESC", (since,)).fetchall()

        per_user, recent = {}, []
        for r in rows:
            end = r["stopped"] if r["stopped"] is not None else now
            # Clip to the window so a session straddling its start only counts
            # the part inside it.
            secs = max(0.0, end - max(r["started"], since))
            u = per_user.setdefault(r["username"], {
                "username": r["username"], "gpu_secs": 0.0, "cpu_secs": 0.0,
                "sessions": 0, "last": 0.0, "running": False})
            u["gpu_secs" if r["gpu"] else "cpu_secs"] += secs
            u["sessions"] += 1
            u["last"] = max(u["last"], end)
            u["running"] = u["running"] or r["stopped"] is None
            if len(recent) < 50:
                recent.append({"username": r["username"],
                               "profile": _size_name(r["size"]) if r["size"] else r["profile"],
                               "gpu": bool(r["gpu"]), "worker": r["kind"] == "worker",
                               "started": r["started"], "stopped": r["stopped"],
                               "secs": end - r["started"], "running": r["stopped"] is None})

        users = sorted(per_user.values(), key=lambda u: (-u["gpu_secs"], -u["cpu_secs"], u["username"]))
        total_gpu = sum(u["gpu_secs"] for u in users)
        total_cpu = sum(u["cpu_secs"] for u in users)

        if self.get_argument("format", "") == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["username", "gpu_hours", "cpu_hours", "sessions", "last_active_utc"])
            for u in users:
                w.writerow([u["username"], f'{u["gpu_secs"]/3600:.2f}', f'{u["cpu_secs"]/3600:.2f}',
                            u["sessions"], datetime.fromtimestamp(u["last"], timezone.utc).isoformat(timespec="seconds")])
            self.set_header("Content-Type", "text/csv; charset=utf-8")
            self.set_header("Content-Disposition", f'attachment; filename="usage-{period}.csv"')
            return self.finish(buf.getvalue())

        def when(ts):
            return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %H:%M")

        max_gpu = max((u["gpu_secs"] for u in users), default=0.0)
        running = sum(1 for u in users if u["running"])
        html = await self.render_template(
            "usage.html", users=users, recent=recent, period=period, periods=self.PERIODS,
            total_gpu=total_gpu, total_cpu=total_cpu, since=since, max_gpu=max_gpu, running=running,
            hours=lambda s: f"{s/3600:.1f}", when=when, dur=_dur, sync=False)
        self.finish(html)


class CliHandler(web.RequestHandler):
    """GET /hub/zai/cli -- the `zai` command-line tool. Public: it is just code."""

    def get(self):
        try:
            with open(CLI_PATH, "rb") as f:
                body = f.read()
        except OSError:
            raise web.HTTPError(404)
        self.set_header("Content-Type", "text/x-python; charset=utf-8")
        self.set_header("Content-Disposition", 'attachment; filename="zai"')
        self.finish(body)


# ---------------------------------------------------------------------------
# JSON API (token or cookie auth, like the rest of /hub/api)
# ---------------------------------------------------------------------------

def _catalog_json(con, user):
    free = availability(con)
    admin_free = bool(user and user.admin and CATALOG.admins_free)
    sizes = []
    for z in CATALOG.sizes:
        n = None
        if z["gpu"]:
            n = free.get(z["gpu"]["pool"], 0) // max(1, z["gpu"]["count"])
        sizes.append({
            "slug": z["slug"], "name": z["name"], "description": z["description"],
            "gpu": z["gpu"], "cpu": z["cpu"], "memory": z["memory"],
            "price_per_hour": 0 if admin_free else z["rate"] / MICROS,
            "available": n, "default": z["default"],
            "templates": [t["slug"] for t in CATALOG.templates_for(z)],
        })
    templates = [{k: t[k] for k in ("slug", "name", "description", "gpu_required", "default")}
                 for t in CATALOG.templates]
    return {"currency": CATALOG.currency, "price_note": CATALOG.price_note,
            "enforce": CATALOG.enforce, "sizes": sizes, "templates": templates,
            "serverless": {"enabled": CATALOG.sv_enabled, "max_gpus": CATALOG.sv_max_gpus,
                           "max_workers_per_endpoint": CATALOG.sv_max_workers}}


class _ZaiAPI(APIHandler):
    def _user(self):
        u = self.current_user
        if u is None or getattr(u, "kind", "user") != "user":
            raise web.HTTPError(403, "A user token is required")
        return u


class CatalogAPI(_ZaiAPI):
    """GET /hub/api/zai/catalog -- sizes, prices, free GPUs, templates."""

    def get(self):
        user = self._user()
        with _db() as con:
            self.finish(_catalog_json(con, user))


class MeAPI(_ZaiAPI):
    """GET /hub/api/zai/me -- the caller's balance and running charges."""

    def get(self):
        user = self._user()
        with _db() as con:
            a = account(con, user.name)
        self.finish({
            "user": user.name, "currency": CATALOG.currency, "enforce": CATALOG.enforce,
            "balance": a["balance"] / MICROS, "accruing": a["accruing"] / MICROS,
            "low": a["balance"] <= CATALOG.low_balance,
            "admin_free": bool(user.admin and CATALOG.admins_free),
            "running": [{"size": r["size"], "template": r["template"], "started": r["started"],
                         "price_per_hour": r["rate"] / MICROS, "cost_so_far": r["cost"] / MICROS}
                        for r in a["running"]],
        })


class CreditsAPI(_ZaiAPI):
    """POST /hub/api/zai/credits {"user", "amount", "kind"?, "note"?} -- admin only.

    The hook for a payment provider's webhook receiver: it posts here with an
    admin-scoped token after a successful charge.
    """

    def post(self):
        user = self._user()
        if not user.admin or "admin:users" not in self.expanded_scopes:
            raise web.HTTPError(403, "Needs an admin token with admin:users")
        body = self.get_json_body() or {}
        name = str(body.get("user", ""))
        if not self.db.query(orm.User).filter_by(name=name).first():
            raise web.HTTPError(404, f"No user {name!r}")
        try:
            micros = to_micros(body.get("amount", ""))
            kind = body.get("kind", "topup")
            if kind == "topup" and micros <= 0:
                raise ValueError("a top-up must be positive")
            bal = add_credit(name, micros, kind, str(body.get("note", "")), user.name)
        except ValueError as ex:
            raise web.HTTPError(400, str(ex))
        self.log.info("billing: %s %s %s for %s via API", user.name, kind, CATALOG.money(micros), name)
        self.set_status(201)
        self.finish({"user": name, "balance": bal / MICROS, "currency": CATALOG.currency})


# ---------------------------------------------------------------------------
# Serverless workers (the gateway, serverless/gateway.py, is the only caller)
#
# The hub stays the one place that admits GPU work: a worker gets its GPU and
# its billing session here, under the same credit and capacity checks as a
# notebook, so the two can never oversubscribe the slices between them.
# ---------------------------------------------------------------------------

WORKER_SCOPE = "custom:zai-workers"
WORKER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")


class _WorkerAPI(APIHandler):
    def _check(self):
        if WORKER_SCOPE not in self.expanded_scopes:
            raise web.HTTPError(403, f"Needs {WORKER_SCOPE}")
        body = self.get_json_body() or {}
        worker = str(body.get("worker", ""))
        if not WORKER_RE.match(worker):
            raise web.HTTPError(400, "bad worker id")
        return body, worker


class WorkerOpenAPI(_WorkerAPI):
    """POST /hub/api/zai/workers/open {"worker", "user", "size", "endpoint"}

    201 {"session"} -- admitted, billing has started
    402             -- owner lacks credit for min_minutes_to_start
    409             -- serverless GPU cap reached (try again later)
    503             -- no free GPU of that size
    """

    def post(self):
        body, worker = self._check()
        if not CATALOG.sv_enabled:
            raise web.HTTPError(403, "serverless is disabled in the catalog")
        name = str(body.get("user", ""))
        u = self.db.query(orm.User).filter_by(name=name).first()
        if not u:
            raise web.HTTPError(404, f"No user {name!r}")
        size = CATALOG.size(str(body.get("size", "")))
        if not size:
            raise web.HTTPError(400, "unknown size")
        rate = 0 if (u.admin and CATALOG.admins_free) else size["rate"]
        server = f"w:{worker}"
        now = time.time()
        with _db() as con:
            if con.execute("SELECT id FROM sessions WHERE server=? AND stopped IS NULL", (server,)).fetchone():
                raise web.HTTPError(409, "worker already has a session")
            if CATALOG.enforce and rate > 0:
                bal = account(con, name, now)["balance"]
                need = int(rate * CATALOG.min_minutes / 60)
                if bal < need:
                    raise web.HTTPError(402, f"{name} has {CATALOG.money(bal)}; a worker needs {CATALOG.money(need)}")
            if size["gpu"]:
                pool, cnt = size["gpu"]["pool"], size["gpu"]["count"]
                if worker_gpus_in_use(con) + cnt > CATALOG.sv_max_gpus:
                    raise web.HTTPError(409, "serverless GPU cap reached")
                if gpus_in_use(con, pool) + cnt > CATALOG.pools[pool]:
                    raise web.HTTPError(503, f"no free {size['name']}")
            cur = con.execute(
                "INSERT INTO sessions(username, server, profile, gpu, started, kind, size, template,"
                " resource, gpus, rate, endpoint) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, server, size["name"], int(bool(size["gpu"])), now, "worker", size["slug"],
                 _endpoint_label(body),
                 size["gpu"]["pool"] if size["gpu"] else "",
                 size["gpu"]["count"] if size["gpu"] else 0, rate,
                 str(body.get("endpoint", ""))[:64]))
        self.log.info("billing: worker %s opened for %s (%s)", worker, name, size["slug"])
        self.set_status(201)
        self.finish({"session": cur.lastrowid, "rate_per_hour": rate / MICROS})


class WorkerCloseAPI(_WorkerAPI):
    """POST /hub/api/zai/workers/close {"worker"} -- idempotent; posts the charge."""

    def post(self):
        body, worker = self._check()
        now = time.time()
        with _db() as con:
            row = con.execute("SELECT username FROM sessions WHERE server=? AND stopped IS NULL",
                              (f"w:{worker}",)).fetchone()
            if row:
                _close_sessions(con, row["username"], f"w:{worker}", now, now)
        self.finish({"closed": bool(row)})


class WorkerAccountsAPI(APIHandler):
    """GET /hub/api/zai/workers/accounts?user=a&user=b -- balances, for the
    gateway to admit jobs and to stop workers whose owner ran out of credit."""

    def get(self):
        if WORKER_SCOPE not in self.expanded_scopes:
            raise web.HTTPError(403, f"Needs {WORKER_SCOPE}")
        names = self.get_arguments("user")[:500]
        out = {}
        with _db() as con:
            for n in names:
                u = self.db.query(orm.User).filter_by(name=n).first()
                if not u:
                    continue
                free = bool(u.admin and CATALOG.admins_free)
                out[n] = {"balance": account(con, n)["balance"] / MICROS, "free": free}
        self.finish({"enforce": CATALOG.enforce, "accounts": out})


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def nav_balance(user):
    """template_vars hook: the balance chip in the nav bar."""
    if user is None or getattr(user, "kind", "user") != "user":
        return None
    if user.admin and CATALOG.admins_free:
        return None
    try:
        with _db() as con:
            bal = account(con, user.name)["balance"]
        return {"text": CATALOG.money(bal), "low": bal <= CATALOG.low_balance}
    except Exception:
        return None


def configure(c):
    global CATALOG
    CATALOG = Catalog.load()
    init_db()

    c.JupyterHub.spawner_class = ZaiSpawner
    c.KubeSpawner.profile_list = CATALOG.profile_list()
    c.ZaiSpawner.options_form = options_form
    c.ZaiSpawner.options_from_form = options_from_form

    tv = dict(c.JupyterHub.get("template_vars", {}) or {})
    tv["zai_balance"] = nav_balance
    tv["zai_serverless"] = CATALOG.sv_enabled
    c.JupyterHub.template_vars = tv

    handlers = list(c.JupyterHub.get("extra_handlers", []) or [])
    handlers += [
        (r"/account", AccountHandler),
        (r"/billing", BillingHandler),
        (r"/usage", UsageHandler),
        (r"/endpoints", EndpointsPage),
        (r"/zai/cli", CliHandler),
        (r"/api/zai/catalog", CatalogAPI),
        (r"/api/zai/me", MeAPI),
        (r"/api/zai/credits", CreditsAPI),
        (r"/api/zai/workers/open", WorkerOpenAPI),
        (r"/api/zai/workers/close", WorkerCloseAPI),
        (r"/api/zai/workers/accounts", WorkerAccountsAPI),
    ]
    c.JupyterHub.extra_handlers = handlers

    scopes = dict(c.JupyterHub.get("custom_scopes", {}) or {})
    scopes[WORKER_SCOPE] = {"description": "Open, close and bill serverless GPU workers (the gateway only)."}
    scopes["custom:serverless"] = {"description": "Call your serverless endpoints (/v2/) as yourself."}
    c.JupyterHub.custom_scopes = scopes
