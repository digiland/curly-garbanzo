#!/usr/bin/env python3
"""ZAI usage meter: who held which resources, minute by minute.

The one writer of usage data. Every SAMPLE_EVERY seconds it lists the running
user-server pods in the JupyterHub namespace and records one row per pod per
minute: user, profile, the GPU slice type, the CPU and memory limits, and how
busy the slice was (from the GPU Operator's DCGM exporter). Hours are counted
rows, so every total can be recomputed from the raw minutes at any time.

Why sampling, not start/stop events: an event that is missed (hub restart,
pod crash, meter restart) turns into a session that never ends or never
started. A sample that is missed loses one minute, and the row key
(pod uid, minute) makes re-recording a minute a no-op.

Gaps: the meter writes a heartbeat row for every minute it successfully listed
pods. A minute with no heartbeat is a minute the meter did not see. When it
comes back, any pod that is still running with the same uid AND was already
running before the gap was provably running throughout, so those minutes are
filled in and flagged `backfilled`. A pod that started and ended entirely
inside a gap cannot be seen; those minutes stay visible as a gap on the admin
page instead of being guessed.

Storage is SQLite on a WEKA volume, written only by this process (one replica,
Recreate strategy). Rollback journal, not WAL: WAL's shared-memory file is not
safe on a network filesystem. A consistent copy is taken daily into
/data/backups and kept BACKUP_KEEP_DAYS.

HTTP API (bearer token; only the hub may connect -- see meter.yaml):
  GET  /livez                         process is up (no auth)
  GET  /healthz                       200 when the last sample is < 2 min old
  GET  /api/usage?month=YYYY-MM       every user's line items for the month
  GET  /api/usage/<user>?month=       one user: items, daily, sessions
  GET  /api/check/<user>              GPU hours used this month vs budget
  GET  /api/export.csv?month=         statement lines, for billing
  POST /api/budget                    {user|"*", gpu_hours|null, by, note}
  POST /api/adjustments               {user, month, resource, hours, reason, by}

Budgets and adjustments are append-only: the latest budget row wins, and a
correction is a new adjustment row (negative hours = credit), never an edit.

Stdlib only, so it runs on the hub image with nothing to build.
"""
import csv
import datetime as dt
import hmac
import io
import json
import logging
import os
import re
import sqlite3
import ssl
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import zoneinfo
except ImportError:  # pragma: no cover
    zoneinfo = None

log = logging.getLogger("meter")

DB_PATH = os.environ.get("METER_DB", "/data/meter.sqlite")
BACKUP_DIR = os.environ.get("METER_BACKUP_DIR", "/data/backups")
BACKUP_KEEP_DAYS = int(os.environ.get("BACKUP_KEEP_DAYS", "35"))
CONFIG_FILE = os.environ.get("METER_CONFIG", "/config/config.json")
NAMESPACE = os.environ.get("METER_NAMESPACE", "jupyterhub")
DCGM_URL = os.environ.get(
    "DCGM_URL", "http://nvidia-dcgm-exporter.gpu-operator-resources.svc:9400/metrics")
SAMPLE_EVERY = float(os.environ.get("SAMPLE_EVERY", "20"))
BACKFILL_MAX_MINUTES = int(os.environ.get("BACKFILL_MAX_MINUTES", str(7 * 24 * 60)))
PORT = int(os.environ.get("PORT", "8000"))
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

USER_RE = re.compile(r"^[a-z][a-z0-9-]{2,30}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    pod_uid     TEXT    NOT NULL,
    minute      INTEGER NOT NULL,          -- unix time // 60
    user        TEXT    NOT NULL,
    profile     TEXT    NOT NULL,
    gpu_resource TEXT,                     -- e.g. mig-1g.35gb; NULL = no GPU
    gpu_count   INTEGER NOT NULL DEFAULT 0,
    cpu         REAL    NOT NULL DEFAULT 0, -- cores (limit)
    mem_gb      REAL    NOT NULL DEFAULT 0, -- GB (limit)
    gpu_busy    REAL,                      -- 0..1, NULL = not measured
    gpu_mem_gb  REAL,
    backfilled  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (pod_uid, minute)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS samples_minute ON samples (minute);
CREATE INDEX IF NOT EXISTS samples_user_minute ON samples (user, minute);

CREATE TABLE IF NOT EXISTS heartbeats (
    minute  INTEGER PRIMARY KEY,
    pods    INTEGER NOT NULL,
    dcgm_ok INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS budgets (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user      TEXT NOT NULL,              -- a username, or '*' for the default
    gpu_hours REAL,                       -- NULL = unlimited
    set_by    TEXT NOT NULL,
    set_at    INTEGER NOT NULL,
    note      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS adjustments (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user     TEXT NOT NULL,
    month    TEXT NOT NULL,               -- YYYY-MM
    resource TEXT NOT NULL,
    hours    REAL NOT NULL,               -- negative = credit
    reason   TEXT NOT NULL,
    set_by   TEXT NOT NULL,
    set_at   INTEGER NOT NULL
);
"""

# --------------------------------------------------------------------------
# config


def load_config(path=CONFIG_FILE):
    """Re-read on every use, so a ConfigMap edit applies without a restart."""
    try:
        with open(path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    cfg.setdefault("timezone", "UTC")
    cfg.setdefault("currency", "USD")
    cfg.setdefault("rates", [])
    cfg.setdefault("default_gpu_budget_hours", None)
    cfg.setdefault("idle_busy_threshold", 0.05)
    cfg.setdefault("resources", {})
    return cfg


def tz_of(cfg):
    if zoneinfo:
        try:
            return zoneinfo.ZoneInfo(cfg["timezone"])
        except Exception:
            log.warning("unknown timezone %r, using UTC", cfg["timezone"])
    return dt.timezone.utc


def rates_for(cfg, month):
    """The newest rate card whose `from` month is <= month; {} if none."""
    best = {}
    for card in sorted(cfg["rates"], key=lambda c: c.get("from", "")):
        if card.get("from", "") <= month:
            best = card.get("per_hour", {})
    return {k: float(v) for k, v in best.items() if v}


# --------------------------------------------------------------------------
# parsing (pure functions; see test_meter.py)

_CPU_RE = re.compile(r"^([0-9.]+)(m?)$")
_MEM_UNITS = {"": 1, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15,
              "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50}
_MEM_RE = re.compile(r"^([0-9.]+(?:e[0-9]+)?)([A-Za-z]{0,2})$")


def parse_cpu(v):
    m = _CPU_RE.match(str(v).strip())
    if not m:
        return 0.0
    n = float(m.group(1))
    return n / 1000 if m.group(2) else n


def parse_mem_gb(v):
    m = _MEM_RE.match(str(v).strip())
    if not m or m.group(2) not in _MEM_UNITS:
        return 0.0
    return float(m.group(1)) * _MEM_UNITS[m.group(2)] / 1e9


def parse_ts(s):
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def parse_pods(pod_list):
    """Running JupyterHub user servers -> the fields a sample needs."""
    out = []
    for p in pod_list.get("items", []):
        md, st, spec = p.get("metadata", {}), p.get("status", {}), p.get("spec", {})
        labels, ann = md.get("labels") or {}, md.get("annotations") or {}
        if labels.get("component") != "singleuser-server":
            continue
        if st.get("phase") != "Running":
            continue   # Pending = queued for a slice, not holding one
        user = ann.get("hub.jupyter.org/username")
        if not user:
            continue
        gpu_res, gpu_n, cpu, mem = None, 0, 0.0, 0.0
        for c in spec.get("containers", []):
            lim = (c.get("resources") or {}).get("limits") or {}
            for k, v in lim.items():
                if k.startswith("nvidia.com/"):
                    gpu_res = k.split("/", 1)[1]
                    gpu_n += int(v)
            cpu += parse_cpu(lim.get("cpu", 0))
            mem += parse_mem_gb(lim.get("memory", 0))
        profile = labels.get("zai/profile") or (f"gpu:{gpu_res}" if gpu_res else "cpu")
        out.append({
            "uid": md["uid"], "name": md.get("name", ""), "user": user,
            "profile": profile, "gpu_resource": gpu_res, "gpu_count": gpu_n,
            "cpu": cpu, "mem_gb": round(mem, 3), "started": parse_ts(st.get("startTime")),
        })
    return out


_LINE_RE = re.compile(r'^(DCGM_FI_[A-Z_]+)\{(.*)\} (\S+)$')
_LAB_RE = re.compile(r'(\w+)="([^"]*)"')


def parse_dcgm(text, namespace):
    """DCGM exporter text -> {pod name: {"busy": 0..1, "mem_gb": GB}}.

    The exporter labels a MIG instance with the pod holding it. Compute
    activity is DCGM_FI_PROF_GR_ENGINE_ACTIVE (fraction of time the slice's
    graphics/compute engine was busy); DEV_GPU_UTIL is the fallback on
    exporters without profiling metrics."""
    busy, util, mem = {}, {}, {}
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        metric, labels, value = m.group(1), dict(_LAB_RE.findall(m.group(2))), m.group(3)
        pod = labels.get("pod")
        if not pod or labels.get("namespace") != namespace:
            continue
        try:
            v = float(value)
        except ValueError:
            continue
        if metric == "DCGM_FI_PROF_GR_ENGINE_ACTIVE":
            busy.setdefault(pod, []).append(v)
        elif metric == "DCGM_FI_DEV_GPU_UTIL":
            util.setdefault(pod, []).append(v / 100)
        elif metric == "DCGM_FI_DEV_FB_USED":
            mem[pod] = mem.get(pod, 0) + v / 1024          # MiB -> GiB-ish
    out = {}
    for pod in set(busy) | set(util) | set(mem):
        vals = busy.get(pod) or util.get(pod)
        out[pod] = {"busy": (sum(vals) / len(vals)) if vals else None,
                    "mem_gb": round(mem[pod], 2) if pod in mem else None}
    return out


# --------------------------------------------------------------------------
# storage


def connect(path=DB_PATH):
    db = sqlite3.connect(path, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def init_db(db):
    db.executescript(SCHEMA)
    db.commit()


_write_lock = threading.Lock()


def record(db, pods, dcgm, now, dcgm_ok=True):
    """Write one sampling pass. Idempotent per (pod, minute)."""
    minute = int(now // 60)
    with _write_lock, db:
        last_hb = db.execute("SELECT max(minute) FROM heartbeats").fetchone()[0]
        gap = last_hb is None or last_hb < minute - 1
        for p in pods:
            g = dcgm.get(p["name"], {}) if p["gpu_count"] else {}
            row = (p["uid"], p["user"], p["profile"], p["gpu_resource"], p["gpu_count"],
                   p["cpu"], p["mem_gb"])
            if gap:
                _backfill(db, p, row, minute)
            db.execute(
                "INSERT OR IGNORE INTO samples (pod_uid, minute, user, profile, gpu_resource,"
                " gpu_count, cpu, mem_gb, gpu_busy, gpu_mem_gb, backfilled)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                (row[0], minute) + row[1:] + (g.get("busy"), g.get("mem_gb")))
        db.execute("INSERT OR IGNORE INTO heartbeats (minute, pods, dcgm_ok) VALUES (?,?,?)",
                   (minute, len(pods), int(bool(dcgm_ok))))


def _backfill(db, p, row, minute):
    """Fill minutes the meter missed while this pod was provably running.

    Only minutes with no heartbeat (the meter was not looking) and only from
    the pod's own start time: a pod with the same uid still Running now, that
    was Running at the start of the gap, held its resources throughout."""
    if not p.get("started"):
        return
    first = max(int(p["started"] // 60) + 1, minute - BACKFILL_MAX_MINUTES)
    if first >= minute:
        return
    seen = {r[0] for r in db.execute(
        "SELECT minute FROM heartbeats WHERE minute >= ? AND minute < ?", (first, minute))}
    missing = [m for m in range(first, minute) if m not in seen]
    db.executemany(
        "INSERT OR IGNORE INTO samples (pod_uid, minute, user, profile, gpu_resource,"
        " gpu_count, cpu, mem_gb, gpu_busy, gpu_mem_gb, backfilled)"
        " VALUES (?,?,?,?,?,?,?,?,NULL,NULL,1)",
        [(row[0], m) + row[1:] for m in missing])
    if missing:
        log.info("backfilled %d minutes for %s (%s)", len(missing), p["user"], p["name"])


def backup(db, now, tz):
    """One consistent copy per local day; prune old ones."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    day = dt.datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d")
    dest = os.path.join(BACKUP_DIR, f"meter-{day}.sqlite")
    if not os.path.exists(dest):
        tmp = dest + ".partial"
        out = sqlite3.connect(tmp)
        with _write_lock:
            db.backup(out)
        out.close()
        os.replace(tmp, dest)
        log.info("backup written: %s", dest)
    cutoff = now - BACKUP_KEEP_DAYS * 86400
    for f in os.listdir(BACKUP_DIR):
        path = os.path.join(BACKUP_DIR, f)
        if f.startswith("meter-") and os.path.getmtime(path) < cutoff:
            os.remove(path)


# --------------------------------------------------------------------------
# reports


def month_bounds(month, tz):
    y, m = map(int, month.split("-"))
    start = dt.datetime(y, m, 1, tzinfo=tz)
    end = dt.datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=tz)
    return int(start.timestamp() // 60), int(end.timestamp() // 60)


def current_month(tz, now=None):
    return dt.datetime.fromtimestamp(now or time.time(), tz).strftime("%Y-%m")


def resource_labels(cfg):
    labels = {"gpu:mig-1g.35gb": "GPU Standard (1g slice)",
              "gpu:mig-2g.35gb": "GPU Large (2g slice)",
              "cpu": "CPU", "mem_gb": "Memory"}
    labels.update(cfg.get("resources", {}))
    return labels


UNITS = {"cpu": "core-hours", "mem_gb": "GB-hours"}


def unit_of(resource):
    return UNITS.get(resource, "slice-hours")


def budget_of(db, user, cfg):
    row = db.execute("SELECT gpu_hours FROM budgets WHERE user = ? ORDER BY id DESC LIMIT 1",
                     (user,)).fetchone()
    if row is None:
        row = db.execute("SELECT gpu_hours FROM budgets WHERE user = '*' ORDER BY id DESC"
                         " LIMIT 1").fetchone()
    if row is not None:
        return row[0]
    return cfg["default_gpu_budget_hours"]


def usage(db, month, cfg, user=None, now=None):
    """Line items per user for a month, from the raw minutes."""
    tz, now = tz_of(cfg), now or time.time()
    lo, hi = month_bounds(month, tz)
    rates, thr = rates_for(cfg, month), cfg["idle_busy_threshold"]
    where, args = "minute >= ? AND minute < ?", [lo, hi]
    if user:
        where += " AND user = ?"
        args.append(user)

    users = {}

    def u(name):
        return users.setdefault(name, {
            "user": name, "items": {}, "adjustments": [], "gpu_hours": 0.0,
            "busy_minutes": 0, "busy_sum": 0.0, "idle_gpu_hours": 0.0,
            "backfilled_hours": 0.0, "sessions": 0, "running": False, "cost": 0.0})

    def add(entry, resource, hours):
        it = entry["items"].setdefault(resource, {"hours": 0.0})
        it["hours"] += hours

    for r in db.execute(
            f"SELECT user, gpu_resource, sum(gpu_count) g, sum(cpu) c, sum(mem_gb) m,"
            f" count(*) n, sum(backfilled) b,"
            f" sum(CASE WHEN gpu_count > 0 AND gpu_busy IS NOT NULL THEN 1 ELSE 0 END) bm,"
            f" sum(CASE WHEN gpu_count > 0 THEN gpu_busy ELSE 0 END) bs,"
            f" sum(CASE WHEN gpu_count > 0 AND gpu_busy < ? THEN gpu_count ELSE 0 END) idle"
            f" FROM samples WHERE {where} GROUP BY user, gpu_resource", [thr] + args):
        e = u(r["user"])
        if r["gpu_resource"]:
            add(e, f"gpu:{r['gpu_resource']}", r["g"] / 60)
            e["gpu_hours"] += r["g"] / 60
        add(e, "cpu", r["c"] / 60)
        add(e, "mem_gb", r["m"] / 60)
        e["busy_minutes"] += r["bm"] or 0
        e["busy_sum"] += r["bs"] or 0
        e["idle_gpu_hours"] += (r["idle"] or 0) / 60
        e["backfilled_hours"] += (r["b"] or 0) / 60

    for r in db.execute(f"SELECT user, count(DISTINCT pod_uid) s, max(minute) last"
                        f" FROM samples WHERE {where} GROUP BY user", args):
        e = u(r["user"])
        e["sessions"] = r["s"]
        e["running"] = r["last"] >= int(now // 60) - 2

    aw, aa = "month = ?", [month]
    if user:
        aw += " AND user = ?"
        aa.append(user)
    for r in db.execute(f"SELECT * FROM adjustments WHERE {aw} ORDER BY id", aa):
        e = u(r["user"])
        e["adjustments"].append({
            "id": r["id"], "resource": r["resource"], "hours": r["hours"],
            "reason": r["reason"], "by": r["set_by"],
            "at": dt.datetime.fromtimestamp(r["set_at"], tz).isoformat(timespec="minutes")})
        add(e, r["resource"], r["hours"])
        if r["resource"].startswith("gpu:"):
            e["gpu_hours"] += r["hours"]

    totals = {"items": {}, "gpu_hours": 0.0, "cost": 0.0}
    for e in users.values():
        for res, it in e["items"].items():
            it["hours"] = round(it["hours"], 4)
            it["unit"] = unit_of(res)
            if res in rates:
                it["rate"] = rates[res]
                it["cost"] = round(it["hours"] * rates[res], 2)
                e["cost"] += it["cost"]
            t = totals["items"].setdefault(res, {"hours": 0.0, "cost": 0.0, "unit": unit_of(res)})
            t["hours"] = round(t["hours"] + it["hours"], 4)
            t["cost"] = round(t["cost"] + it.get("cost", 0), 2)
        e["gpu_busy_avg"] = round(e["busy_sum"] / e["busy_minutes"], 3) if e["busy_minutes"] else None
        del e["busy_sum"], e["busy_minutes"]
        for k in ("gpu_hours", "idle_gpu_hours", "backfilled_hours", "cost"):
            e[k] = round(e[k], 2 if k == "cost" else 4)
        e["budget"] = budget_of(db, e["user"], cfg)
        totals["gpu_hours"] = round(totals["gpu_hours"] + e["gpu_hours"], 4)
        totals["cost"] = round(totals["cost"] + e["cost"], 2)

    return {
        "month": month, "timezone": cfg["timezone"], "currency": cfg["currency"],
        "priced": bool(rates), "rates": rates, "resources": resource_labels(cfg),
        "default_budget": budget_of(db, "*", cfg),
        "users": sorted(users.values(), key=lambda e: (-e["gpu_hours"], e["user"])),
        "totals": totals,
    }


def user_detail(db, name, month, cfg, now=None):
    tz, now = tz_of(cfg), now or time.time()
    lo, hi = month_bounds(month, tz)
    rep = usage(db, month, cfg, user=name, now=now)
    rep["user"] = next((e for e in rep["users"] if e["user"] == name), None)
    rep["budget"] = budget_of(db, name, cfg)
    daily = {}
    for r in db.execute(
            "SELECT minute / 60 AS hr, sum(gpu_count) g, sum(cpu) c, count(*) n"
            " FROM samples WHERE user = ? AND minute >= ? AND minute < ? GROUP BY hr",
            (name, lo, hi)):
        day = dt.datetime.fromtimestamp(r["hr"] * 3600, tz).strftime("%Y-%m-%d")
        d = daily.setdefault(day, {"date": day, "gpu_hours": 0.0, "cpu_hours": 0.0, "hours": 0.0})
        d["gpu_hours"] += r["g"] / 60
        d["cpu_hours"] += r["c"] / 60
        d["hours"] += r["n"] / 60
    rep["daily"] = [{k: round(v, 3) if isinstance(v, float) else v for k, v in d.items()}
                    for d in sorted(daily.values(), key=lambda d: d["date"])]
    sessions = []
    for r in db.execute(
            "SELECT pod_uid, profile, gpu_resource, min(minute) a, max(minute) b, count(*) n,"
            " avg(gpu_busy) busy, sum(backfilled) bf FROM samples"
            " WHERE user = ? AND minute >= ? AND minute < ? GROUP BY pod_uid ORDER BY a DESC",
            (name, lo, hi)):
        sessions.append({
            "profile": r["profile"], "gpu_resource": r["gpu_resource"],
            "start": dt.datetime.fromtimestamp(r["a"] * 60, tz).isoformat(timespec="minutes"),
            "end": dt.datetime.fromtimestamp((r["b"] + 1) * 60, tz).isoformat(timespec="minutes"),
            "minutes": r["n"], "backfilled_minutes": r["bf"] or 0,
            "gpu_busy_avg": round(r["busy"], 3) if r["busy"] is not None else None,
            "running": r["b"] >= int(now // 60) - 2,
        })
    rep["sessions"] = sessions
    rep["budget_history"] = [
        {"gpu_hours": r["gpu_hours"], "by": r["set_by"], "note": r["note"],
         "at": dt.datetime.fromtimestamp(r["set_at"], tz).isoformat(timespec="minutes")}
        for r in db.execute("SELECT * FROM budgets WHERE user = ? ORDER BY id DESC LIMIT 20",
                            (name,))]
    return rep


def gpu_used_this_month(db, name, cfg, now=None):
    tz = tz_of(cfg)
    month = current_month(tz, now)
    lo, hi = month_bounds(month, tz)
    g = db.execute("SELECT coalesce(sum(gpu_count), 0) FROM samples"
                   " WHERE user = ? AND minute >= ? AND minute < ?", (name, lo, hi)).fetchone()[0]
    adj = db.execute("SELECT coalesce(sum(hours), 0) FROM adjustments"
                     " WHERE user = ? AND month = ? AND resource LIKE 'gpu:%'",
                     (name, month)).fetchone()[0]
    return month, round(g / 60 + adj, 4)


def meter_status(db, month, cfg, now=None):
    """Freshness, and the minutes this month the meter was not looking."""
    tz, now = tz_of(cfg), now or time.time()
    lo, hi = month_bounds(month, tz)
    first = db.execute("SELECT min(minute) FROM heartbeats").fetchone()[0]
    last = db.execute("SELECT max(minute) FROM heartbeats").fetchone()[0]
    now_min = int(now // 60)
    gaps = []
    if first is not None:
        # Expected: a heartbeat for every minute in [start, end). The current
        # minute may not be written yet, so it is the exclusive end.
        start, end = max(lo, first), min(hi, now_min)
        prev = start - 1
        rows = [tuple(r) for r in db.execute(
            "SELECT minute, dcgm_ok FROM heartbeats WHERE minute >= ? AND minute < ?"
            " ORDER BY minute", (start, end))]
        for (m, _) in rows + [(end, 1)]:
            if m - prev > 2:        # one missed minute is sampling jitter, not a gap
                gaps.append({
                    "from": dt.datetime.fromtimestamp((prev + 1) * 60, tz).isoformat(timespec="minutes"),
                    "to": dt.datetime.fromtimestamp(m * 60, tz).isoformat(timespec="minutes"),
                    "minutes": m - prev - 1})
            prev = m
        dcgm_bad = sum(1 for r in rows if not r[1])
    else:
        dcgm_bad = 0
    bf = db.execute("SELECT count(*) FROM samples WHERE backfilled = 1 AND minute >= ? AND minute < ?",
                    (lo, hi)).fetchone()[0]
    return {
        # Lower bound: the minute's heartbeat was written somewhere inside it.
        "last_sample_age_s": None if last is None else max(0, int(now - last * 60 - 60)),
        "metering_since": None if first is None else
            dt.datetime.fromtimestamp(first * 60, tz).isoformat(timespec="minutes"),
        "gaps": gaps, "gap_minutes": sum(g["minutes"] for g in gaps),
        "backfilled_minutes": bf, "gpu_metrics_missing_minutes": dcgm_bad,
    }


def months_available(db, cfg, now=None):
    tz = tz_of(cfg)
    first = db.execute("SELECT min(minute) FROM heartbeats").fetchone()[0]
    cur = current_month(tz, now)
    if first is None:
        return [cur]
    start = dt.datetime.fromtimestamp(first * 60, tz)
    y, m = start.year, start.month
    out = []
    while f"{y:04d}-{m:02d}" <= cur:
        out.append(f"{y:04d}-{m:02d}")
        y, m = y + (m == 12), m % 12 + 1
    return list(reversed(out))[:24]


def export_csv(db, month, cfg):
    rep = usage(db, month, cfg)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["month", "user", "resource", "description", "quantity", "unit",
                "rate_per_hour", "amount", "currency", "note"])
    for e in rep["users"]:
        for res, it in sorted(e["items"].items()):
            if abs(it["hours"]) < 1e-9:
                continue
            w.writerow([month, e["user"], res, rep["resources"].get(res, res),
                        f"{it['hours']:.4f}", it["unit"], it.get("rate", ""),
                        f"{it['cost']:.2f}" if "cost" in it else "", rep["currency"],
                        "includes adjustments" if any(a["resource"] == res for a in e["adjustments"]) else ""])
    return buf.getvalue()


# --------------------------------------------------------------------------
# writes from the API


def set_budget(db, body):
    user = body.get("user", "")
    if user != "*" and not USER_RE.match(user):
        raise ValueError("user must be a username or '*'")
    hours = body.get("gpu_hours")
    if hours is not None:
        hours = float(hours)
        if not 0 <= hours <= 100000:
            raise ValueError("gpu_hours must be between 0 and 100000, or null for unlimited")
    by = str(body.get("by", ""))[:64]
    if not by:
        raise ValueError("by is required")
    with _write_lock, db:
        db.execute("INSERT INTO budgets (user, gpu_hours, set_by, set_at, note) VALUES (?,?,?,?,?)",
                   (user, hours, by, int(time.time()), str(body.get("note", ""))[:200]))


def add_adjustment(db, body, cfg):
    user, month, res = body.get("user", ""), body.get("month", ""), body.get("resource", "")
    if not USER_RE.match(user):
        raise ValueError("invalid user")
    if not MONTH_RE.match(month):
        raise ValueError("month must be YYYY-MM")
    if res not in resource_labels(cfg):
        raise ValueError(f"resource must be one of {sorted(resource_labels(cfg))}")
    hours = float(body.get("hours", 0))
    if hours == 0 or abs(hours) > 100000:
        raise ValueError("hours must be non-zero (negative for a credit)")
    reason, by = str(body.get("reason", "")).strip()[:200], str(body.get("by", ""))[:64]
    if not reason or not by:
        raise ValueError("reason and by are required")
    with _write_lock, db:
        db.execute("INSERT INTO adjustments (user, month, resource, hours, reason, set_by, set_at)"
                   " VALUES (?,?,?,?,?,?,?)", (user, month, res, hours, reason, by, int(time.time())))


# --------------------------------------------------------------------------
# sampling loop


class Kube:
    def __init__(self):
        self.base = "https://kubernetes.default.svc"
        self.ctx = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")

    def list_pods(self):
        with open(f"{SA_DIR}/token") as f:          # re-read: tokens rotate
            token = f.read().strip()
        req = urllib.request.Request(
            f"{self.base}/api/v1/namespaces/{NAMESPACE}/pods"
            "?labelSelector=component%3Dsingleuser-server",
            headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, context=self.ctx, timeout=10) as r:
            return json.load(r)


def fetch_dcgm():
    with urllib.request.urlopen(DCGM_URL, timeout=5) as r:
        return r.read().decode()


def sampler(db, stop):
    kube = Kube()
    while not stop.is_set():
        t0 = time.time()
        try:
            pods = parse_pods(kube.list_pods())
        except Exception as e:
            log.error("listing pods failed, minute not recorded: %s", e)
            stop.wait(SAMPLE_EVERY)
            continue
        dcgm, ok = {}, True
        if any(p["gpu_count"] for p in pods):
            try:
                dcgm = parse_dcgm(fetch_dcgm(), NAMESPACE)
            except Exception as e:
                ok = False
                log.warning("DCGM exporter unreachable, GPU busy not recorded: %s", e)
        try:
            record(db, pods, dcgm, t0, ok)
            backup(db, t0, tz_of(load_config()))
        except Exception:
            log.exception("recording failed")
        stop.wait(max(1.0, SAMPLE_EVERY - (time.time() - t0)))


# --------------------------------------------------------------------------
# HTTP


def make_handler(open_db, token):
    """Each request gets its own connection: one sqlite3 connection shared
    across threads would interleave their transactions."""
    class H(BaseHTTPRequestHandler):
        server_version = "zai-meter"

        def log_message(self, fmt, *args):
            if not self.path.startswith(("/livez", "/healthz")):
                log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else (
                body.encode() if isinstance(body, str) else json.dumps(body).encode())
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authed(self):
            got = self.headers.get("Authorization", "")
            if hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
                return True
            self._send(401, {"error": "unauthorized"})
            return False

        def _month(self, q, cfg):
            month = (q.get("month") or [current_month(tz_of(cfg))])[0]
            if not MONTH_RE.match(month):
                raise ValueError("month must be YYYY-MM")
            return month

        def do_GET(self):
            db = open_db()
            try:
                self._get(db)
            finally:
                db.close()

        def do_POST(self):
            db = open_db()
            try:
                self._post(db)
            finally:
                db.close()

        def _get(self, db):
            url = urlparse(self.path)
            q, path = parse_qs(url.query), url.path.rstrip("/")
            cfg = load_config()
            try:
                if path == "/livez":
                    return self._send(200, {"ok": True})
                if path == "/healthz":
                    st = meter_status(db, current_month(tz_of(cfg)), cfg)
                    age = st["last_sample_age_s"]
                    ok = age is not None and age < 120
                    return self._send(200 if ok else 503, {"ok": ok, "last_sample_age_s": age})
                if not self._authed():
                    return
                if path == "/api/usage":
                    month = self._month(q, cfg)
                    rep = usage(db, month, cfg)
                    rep["meter"] = meter_status(db, month, cfg)
                    rep["months"] = months_available(db, cfg)
                    return self._send(200, rep)
                m = re.match(r"^/api/usage/([a-z][a-z0-9-]{2,30})$", path)
                if m:
                    month = self._month(q, cfg)
                    rep = user_detail(db, m.group(1), month, cfg)
                    rep["meter"] = meter_status(db, month, cfg)
                    rep["months"] = months_available(db, cfg)
                    return self._send(200, rep)
                m = re.match(r"^/api/check/([a-z][a-z0-9-]{2,30})$", path)
                if m:
                    month, used = gpu_used_this_month(db, m.group(1), cfg)
                    budget = budget_of(db, m.group(1), cfg)
                    return self._send(200, {
                        "month": month, "gpu_hours_used": used, "budget": budget,
                        "allowed": budget is None or used < budget})
                if path == "/api/export.csv":
                    return self._send(200, export_csv(db, self._month(q, cfg), cfg),
                                      "text/csv; charset=utf-8")
                return self._send(404, {"error": "not found"})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception:
                log.exception("GET %s failed", self.path)
                return self._send(500, {"error": "internal error"})

        def _post(self, db):
            if not self._authed():
                return
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n > 65536:
                    return self._send(413, {"error": "too large"})
                body = json.loads(self.rfile.read(n) or b"{}")
                path = urlparse(self.path).path.rstrip("/")
                if path == "/api/budget":
                    set_budget(db, body)
                elif path == "/api/adjustments":
                    add_adjustment(db, body, load_config())
                else:
                    return self._send(404, {"error": "not found"})
                log.info("%s by %s: %s", path, body.get("by"), json.dumps(body)[:300])
                return self._send(200, {"ok": True})
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)})
            except Exception:
                log.exception("POST %s failed", self.path)
                return self._send(500, {"error": "internal error"})

    return H


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("METER_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("METER_TOKEN must be set (32+ chars)")
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = connect()
    init_db(db)
    stop = threading.Event()
    threading.Thread(target=sampler, args=(db, stop), daemon=True, name="sampler").start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(connect, token))
    log.info("meter listening on :%d, sampling %s every %ss", PORT, NAMESPACE, SAMPLE_EVERY)
    try:
        srv.serve_forever()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
