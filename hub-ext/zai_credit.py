"""Credit requests: a user asks for more credit, an administrator approves it.

There is no online payment. A user asks on /hub/account (amount, how they
will pay, a payment reference if they have one); an administrator sends them
payment details, and once the money has arrived approves the request on
/hub/billing, which adds the credit as an ordinary top-up in zai_platform's
ledger. See docs/CREDIT-REQUESTS.md. Loaded by charts/jupyterhub-values.yaml,
03-zai-platform, after zai_platform (whose catalog, ledger and database this
uses).

Requests are kept in zai_credit_requests, next to the ledger, and never
deleted. A user has at most one waiting request; the database enforces it.
Approving and writing the ledger row happen in one transaction, so a request
cannot be paid twice.
"""
import re
import sqlite3
import time

from jupyterhub import orm
from jupyterhub.handlers.base import BaseHandler
from nativeauthenticator.orm import UserInfo
from tornado import web
from traitlets.log import get_logger

import zai_platform

_CR_PRESETS = ["10", "25", "50", "100"]
_CR_MIN, _CR_MAX = 5, 1000
_CR_METHODS = {"ecocash": "EcoCash", "bank": "Bank transfer", "invoice": "Invoice (organisations)"}
_CR_AMOUNT = re.compile(r"^\d{1,4}(\.\d{1,2})?$")
_CR_REFERENCE_MAX, _CR_NOTE_MAX, _CR_REASON_MAX = 100, 500, 300


def _cr_init():
    with zai_platform._db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS zai_credit_requests (
                id         INTEGER PRIMARY KEY,
                username   TEXT NOT NULL,
                email      TEXT NOT NULL DEFAULT '',
                micros     INTEGER NOT NULL,           -- asked for
                method     TEXT NOT NULL,
                reference  TEXT NOT NULL DEFAULT '',
                note       TEXT NOT NULL DEFAULT '',
                status     TEXT NOT NULL,              -- pending | approved | declined | cancelled
                created    REAL NOT NULL,
                decided    REAL,
                decided_by TEXT NOT NULL DEFAULT '',
                granted    INTEGER NOT NULL DEFAULT 0, -- micros added, when approved
                reason     TEXT NOT NULL DEFAULT '',   -- why, when declined
                ledger_id  INTEGER
            )""")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_credit_requests_one_pending"
                    " ON zai_credit_requests(username) WHERE status='pending'")
        con.execute("CREATE INDEX IF NOT EXISTS ix_credit_requests_user"
                    " ON zai_credit_requests(username, created)")


def _cr_validate(amount, method, reference, note):
    """(micros, method, reference, note) cleaned, or ValueError with what is wrong."""
    amount = amount.strip().lstrip("$").strip()
    if not _CR_AMOUNT.match(amount):
        raise ValueError("Enter an amount in dollars, such as 25 or 25.50.")
    micros = zai_platform.to_micros(amount)
    if not _CR_MIN * zai_platform.MICROS <= micros <= _CR_MAX * zai_platform.MICROS:
        raise ValueError(f"Ask for between ${_CR_MIN} and ${_CR_MAX:,}.")
    if method not in _CR_METHODS:
        raise ValueError("Choose how you will pay.")
    reference, note = reference.strip(), note.strip()
    if len(reference) > _CR_REFERENCE_MAX:
        raise ValueError(f"The payment reference must be {_CR_REFERENCE_MAX} characters or fewer.")
    if len(note) > _CR_NOTE_MAX:
        raise ValueError(f"The note must be {_CR_NOTE_MAX} characters or fewer.")
    return micros, method, reference, note


def _cr_free(user):
    """True for an administrator whose servers are not charged."""
    return bool(user.admin and zai_platform.CATALOG.admins_free)


def _cr_create(username, email, micros, method, reference, note):
    """Store a waiting request; ValueError if the user already has one."""
    try:
        with zai_platform._db() as con:
            return con.execute(
                "INSERT INTO zai_credit_requests(username, email, micros, method, reference, note,"
                " status, created) VALUES (?,?,?,?,?,?,'pending',?)",
                (username, email, micros, method, reference, note, time.time())).lastrowid
    except sqlite3.IntegrityError:
        raise ValueError("You already have a request waiting. Cancel it to ask for a different amount.")


def _cr_cancel(username):
    """Withdraw the user's waiting request, if any."""
    with zai_platform._db() as con:
        con.execute("UPDATE zai_credit_requests SET status='cancelled', decided=?, decided_by=?"
                    " WHERE username=? AND status='pending'", (time.time(), username, username))


def _cr_decide(rid, admin, action, amount="", reference="", reason="", user_exists=lambda name: True):
    """Approve or decline a waiting request. Returns the request as it was;
    ValueError with what is wrong.

    Approving marks the request and writes the ledger top-up in ONE
    transaction, and only while the request is still waiting, so two admins
    pressing Approve at once cannot pay it twice."""
    now = time.time()
    with zai_platform._db() as con:
        req = con.execute("SELECT * FROM zai_credit_requests WHERE id=?", (rid,)).fetchone()
        if req is None:
            raise ValueError("No such request.")
        if req["status"] != "pending":
            raise ValueError(f"This request was already {req['status']}.")
        if action == "approve":
            try:
                granted = zai_platform.to_micros(amount.strip().lstrip("$").strip())
            except ValueError:
                raise ValueError("Enter the amount to add, such as 25.00.")
            if granted <= 0:
                raise ValueError("The amount to add must be more than zero.")
            if not user_exists(req["username"]):
                raise ValueError(f"There is no user {req['username']!r} any more; decline the request.")
            reference = reference.strip()[:_CR_REFERENCE_MAX] or req["reference"]
            note = f"Credit request #{rid}"
            if reference:
                note += f" · {_CR_METHODS.get(req['method'], req['method'])} ref {reference}"
            cur = con.execute(
                "UPDATE zai_credit_requests SET status='approved', decided=?, decided_by=?, granted=?,"
                " reference=? WHERE id=? AND status='pending'", (now, admin, granted, reference, rid))
            if cur.rowcount != 1:
                raise ValueError("This request was already decided.")
            lid = con.execute(
                "INSERT INTO ledger(username, micros, kind, note, actor, ts) VALUES (?,?,?,?,?,?)",
                (req["username"], granted, "topup", note[:200], admin, now)).lastrowid
            con.execute("UPDATE zai_credit_requests SET ledger_id=? WHERE id=?", (lid, rid))
        elif action == "decline":
            reason = reason.strip()
            if not reason:
                raise ValueError("Say why the request is declined; the user sees it.")
            cur = con.execute(
                "UPDATE zai_credit_requests SET status='declined', decided=?, decided_by=?, reason=?"
                " WHERE id=? AND status='pending'", (now, admin, reason[:_CR_REASON_MAX], rid))
            if cur.rowcount != 1:
                raise ValueError("This request was already decided.")
        else:
            raise ValueError("Approve or decline.")
        return dict(req)


class CreditRequestHandler(BaseHandler):
    """POST /hub/credit/request -- a user asks for credit."""

    @web.authenticated
    async def post(self):
        user = self.current_user
        if _cr_free(user):
            raise web.HTTPError(400, "Your servers are not charged; you do not need credit.")
        info = UserInfo.find(self.db, user.name)
        try:
            micros, method, reference, note = _cr_validate(
                self.get_body_argument("amount", ""), self.get_body_argument("method", ""),
                self.get_body_argument("reference", ""), self.get_body_argument("note", ""))
            _cr_create(user.name, (info.email or "") if info else "", micros, method, reference, note)
        except ValueError as e:
            raise web.HTTPError(400, str(e))
        self.log.info("credit request: %s asked for %s by %s", user.name,
                      zai_platform.CATALOG.money(micros), method)
        self.redirect(self.hub.base_url + "account#credit")


class CreditCancelHandler(BaseHandler):
    """POST /hub/credit/cancel -- a user withdraws their waiting request."""

    @web.authenticated
    async def post(self):
        _cr_cancel(self.current_user.name)
        self.redirect(self.hub.base_url + "account#credit")


class CreditDecideHandler(BaseHandler):
    """POST /hub/credit/decide -- an administrator approves or declines."""

    @web.authenticated
    async def post(self):
        admin = self.current_user
        if not admin.admin:
            raise web.HTTPError(403, "Administrators only")
        action = self.get_body_argument("action", "")
        rid = self.get_body_argument("id", "")
        if not rid.isdigit():
            raise web.HTTPError(400, "No such request.")
        rid = int(rid)
        try:
            req = _cr_decide(
                rid, admin.name, action, amount=self.get_body_argument("amount", ""),
                reference=self.get_body_argument("reference", ""),
                reason=self.get_body_argument("reason", ""),
                user_exists=lambda name: self.db.query(orm.User).filter_by(name=name).first() is not None)
        except ValueError as e:
            raise web.HTTPError(400, str(e))
        self.log.info("credit request #%d for %s: %s by %s", rid, req["username"], action, admin.name)
        self.redirect(self.hub.base_url + "billing#credit-requests")


def _cr_view(user):
    """template_vars: what the Account, Billing and nav templates show, or None."""
    if user is None or getattr(user, "kind", "user") != "user":
        return None
    try:
        cat = zai_platform.CATALOG
        with zai_platform._db() as con:
            mine = con.execute("SELECT * FROM zai_credit_requests WHERE username=?"
                               " ORDER BY id DESC LIMIT 1", (user.name,)).fetchone()
            view = {
                "free": _cr_free(user), "mine": dict(mine) if mine else None,
                "presets": _CR_PRESETS, "min": _CR_MIN, "max": _CR_MAX, "methods": _CR_METHODS,
                "reference_max": _CR_REFERENCE_MAX, "note_max": _CR_NOTE_MAX,
                "symbol": cat.symbol, "vat_percent": cat.vat_percent,
                # For "$25 is about 19 hours on ...": the GPU sizes, cheapest first.
                "sizes": sorted(({"name": z["name"], "rate": z["rate"] / zai_platform.MICROS}
                                 for z in cat.sizes if z["gpu"] and z["rate"] > 0), key=lambda z: z["rate"]),
                "pending": [], "recent": [], "pending_count": 0,
            }
            if user.admin:
                view["pending"] = [dict(r) for r in con.execute(
                    "SELECT * FROM zai_credit_requests WHERE status='pending' ORDER BY id")]
                view["recent"] = [dict(r) for r in con.execute(
                    "SELECT * FROM zai_credit_requests WHERE status!='pending' ORDER BY decided DESC LIMIT 10")]
                view["pending_count"] = len(view["pending"])
        return view
    except Exception as e:
        get_logger().error("credit requests: %s", e)
        return None


def configure(c):
    _cr_init()
    handlers = list(c.JupyterHub.get("extra_handlers", []) or [])
    handlers += [
        (r"/credit/request", CreditRequestHandler),
        (r"/credit/cancel", CreditCancelHandler),
        (r"/credit/decide", CreditDecideHandler),
    ]
    c.JupyterHub.extra_handlers = handlers
    # JupyterHub calls any callable in template_vars with the current user and
    # hands the template the result.
    tv = dict(c.JupyterHub.get("template_vars", {}) or {})
    tv["zai_credit"] = _cr_view
    c.JupyterHub.template_vars = tv
