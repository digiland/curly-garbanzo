"""My usage: each user's own hours, spend and sessions, at /hub/my-usage.

The admin Usage page (/hub/usage, zai_platform) shows everyone's hours; the
Account page shows a user's last 100 ledger rows. This page gives a user their
own totals for a period, a breakdown by size, every session, and two CSV
downloads: their sessions, and a statement of charges and credits. Loaded by
charts/jupyterhub-values.yaml, 03-zai-platform, after zai_platform (whose
catalog and database it reads; it writes nothing).

A session that crosses the start of the period counts only its part inside
it, as on the admin page, and its cost is that part at the session's price.
Running sessions count up to now.
"""
import csv
import io
import time
from datetime import datetime, timezone

from jupyterhub.handlers.base import BaseHandler
from tornado import web

import zai_platform

PERIODS = {"month": "This month", "last-month": "Last month", "30d": "Last 30 days", "all": "All time"}
_MU_SHOWN = 200       # sessions listed on the page; the CSV has them all


def _mu_window(period, now):
    """(since, until) in epoch seconds for a period key."""
    first = datetime.fromtimestamp(now, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "month":
        return first.timestamp(), now
    if period == "last-month":
        prev = (first.replace(year=first.year - 1, month=12) if first.month == 1
                else first.replace(month=first.month - 1))
        return prev.timestamp(), first.timestamp()
    if period == "30d":
        return now - 30 * 86400, now
    return 0.0, now


def _mu_report(con, username, since, until, now):
    """The user's sessions in [since, until), each clipped to it, with totals."""
    rows = con.execute(
        "SELECT * FROM sessions WHERE username=? AND started < ? AND (stopped IS NULL OR stopped > ?)"
        " ORDER BY started DESC", (username, until, since)).fetchall()
    sessions, by_size = [], {}
    totals = {"gpu_secs": 0.0, "cpu_secs": 0.0, "cost": 0, "sessions": 0, "running": 0}
    for r in rows:
        end = r["stopped"] if r["stopped"] is not None else now
        secs = max(0.0, min(end, until) - max(r["started"], since))
        cost = zai_platform.session_cost(r["rate"], 0, secs)
        name = zai_platform._size_name(r["size"]) if r["size"] else (r["profile"] or "Unknown")
        worker = r["kind"] == "worker"
        sessions.append({
            "size": name, "template": r["template"], "worker": worker, "gpu": bool(r["gpu"]),
            "started": r["started"], "stopped": r["stopped"], "running": r["stopped"] is None,
            "secs": secs, "rate": r["rate"], "cost": cost})
        totals["gpu_secs" if r["gpu"] else "cpu_secs"] += secs
        totals["cost"] += cost
        totals["sessions"] += 1
        totals["running"] += r["stopped"] is None
        key = ("Serverless · " if worker else "") + name
        z = by_size.setdefault(key, {"size": key, "gpu": bool(r["gpu"]), "sessions": 0, "secs": 0.0, "cost": 0})
        z["sessions"] += 1
        z["secs"] += secs
        z["cost"] += cost
    totals["credit"] = con.execute(
        "SELECT COALESCE(SUM(micros),0) FROM ledger WHERE username=? AND kind!='usage' AND micros>0"
        " AND ts>=? AND ts<?", (username, since, until)).fetchone()[0]
    sizes = sorted(by_size.values(), key=lambda z: (-z["secs"], z["size"]))
    return sessions, sizes, totals


def _utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else ""


class MyUsageHandler(BaseHandler):
    """GET /hub/my-usage -- the signed-in user's own usage, HTML or CSV."""

    @web.authenticated
    async def get(self):
        user = self.current_user
        cat = zai_platform.CATALOG
        period = self.get_argument("period", "month")
        if period not in PERIODS:
            period = "month"
        now = time.time()
        since, until = _mu_window(period, now)
        with zai_platform._db() as con:
            sessions, sizes, totals = _mu_report(con, user.name, since, until, now)
            balance = zai_platform.account(con, user.name, now)["balance"]
            statement = [dict(r) for r in con.execute(
                "SELECT * FROM ledger WHERE username=? AND ts>=? AND ts<? ORDER BY ts",
                (user.name, since, until))] if self.get_argument("format", "") == "statement" else []

        fmt = self.get_argument("format", "")
        if fmt in ("csv", "statement"):
            buf = io.StringIO()
            w = csv.writer(buf)
            if fmt == "csv":
                w.writerow(["started_utc", "stopped_utc", "size", "template", "kind", "hours_in_period",
                            "price_per_hour", "cost_in_period", "currency"])
                for s in reversed(sessions):
                    w.writerow([_utc(s["started"]), "running" if s["running"] else _utc(s["stopped"]),
                                s["size"], s["template"], "serverless" if s["worker"] else "notebook",
                                f"{s['secs'] / 3600:.4f}", f"{s['rate'] / zai_platform.MICROS:.6f}",
                                f"{s['cost'] / zai_platform.MICROS:.6f}", cat.currency])
                name = f"my-usage-{period}.csv"
            else:
                w.writerow(["time_utc", "kind", "description", "amount", "currency"])
                for r in statement:
                    w.writerow([_utc(r["ts"]), r["kind"], r["note"],
                                f"{r['micros'] / zai_platform.MICROS:.6f}", cat.currency])
                name = f"my-statement-{period}.csv"
            self.set_header("Content-Type", "text/csv; charset=utf-8")
            self.set_header("Content-Disposition", f'attachment; filename="{name}"')
            return self.finish(buf.getvalue())

        html = await self.render_template(
            "my-usage.html", sessions=sessions[:_MU_SHOWN], more=max(0, len(sessions) - _MU_SHOWN),
            sizes=sizes, totals=totals, balance=balance, period=period, periods=PERIODS,
            since=since, until=until, catalog=cat, money=cat.money, rate_str=cat.rate_str,
            admin_free=bool(user.admin and cat.admins_free),
            hours=lambda s: f"{s / 3600:,.1f}", when=zai_platform._when, dur=zai_platform._dur, sync=False)
        self.finish(html)


def configure(c):
    handlers = list(c.JupyterHub.get("extra_handlers", []) or [])
    handlers.append((r"/my-usage", MyUsageHandler))
    c.JupyterHub.extra_handlers = handlers
