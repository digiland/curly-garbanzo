"""Online payment for credit, through Econet's Eco Payments API (Sasai).

Off unless ECO_PAYMENTS_CLIENT_ID and ECO_PAYMENTS_CLIENT_SECRET are set.
With them, the credit form on /hub/account offers "Pay now" methods next to
the manual ones (zai_credit). See docs/PAYMENTS.md. Loaded by
charts/jupyterhub-values.yaml, 03-zai-platform, after zai_credit.

A payment, start to finish:

1. The user picks an amount and a method. zai_credit stores a waiting credit
   request with method "online"; this module creates the payment
   (POST /payments) and keeps it in zai_payments, next to the ledger.
2. The answer's nextAction says what the payer does. Live, every method
   answers CHECKOUT with nextAction.checkout.url: Sasai's hosted checkout,
   where the payer enters card details or approves EcoCash. (The Postman
   collection also shows REDIRECT / redirectUrl and USSD_PUSH.)
3. The payment is settled from GET /payments/{id} only: when the payer comes
   back, when they or an admin press Check payment, when the provider's
   webhook arrives (a nudge to look, never trusted for the status itself),
   and every ECO_PAYMENTS_POLL_SECONDS in the background.
4. SUCCEEDED adds the credit and approves the request, in one transaction,
   once: a payment is credited at most one time however many checks race.
   If the provider reports an amount other than the one asked for, nothing
   is credited and the payment waits as AMOUNT_MISMATCH for an administrator.
   A payment that succeeds after its request was cancelled is still credited:
   the money arrived. FAILED / EXPIRED decline the request so the user can
   try again. Still pending after seven days: given up and the request
   cancelled.

Refunds stay a manual adjustment on /hub/billing: the API answers 501 for
Sasai-backed payments.
"""
import asyncio
import json
import os
import re
import secrets
import time
import uuid
from urllib.parse import quote, urlparse

from jupyterhub.handlers.base import BaseHandler
from nativeauthenticator.orm import UserInfo
from tornado import web
from tornado.httpclient import AsyncHTTPClient, HTTPRequest
from traitlets.log import get_logger

import zai_credit
import zai_platform

# The methods this module knows how to take. Their names are replaced by the
# provider's own (displayName) once /payment-methods has answered.
_PAY_KNOWN = ("SASAI_WALLET", "CARD", "ECOCASH")
_PAY_DEFAULT_NAMES = {"SASAI_WALLET": "Sasai wallet", "CARD": "Card", "ECOCASH": "EcoCash"}
_PAY_LABELS = dict(_PAY_DEFAULT_NAMES)
_PAY_DEFAULT_URL = "https://api-prod.developer.econet.co.zw/eco-payments/v1"
_PAY_FINAL = {"SUCCEEDED", "FAILED", "EXPIRED", "REFUNDED"}
_PAY_GIVE_UP = 7 * 86400            # stop asking about a payment this old
_PAY_CHECK_GAP = 5                  # seconds between checks of one payment
_PAY_MOBILE = re.compile(r"^\+?263[0-9]{9}$")

# Settings, read by configure(); `on` is False until both credentials are set.
_PAY = {"on": False, "url": _PAY_DEFAULT_URL, "id": "", "secret": "", "public": "",
        "poll": 60.0, "methods": list(_PAY_LABELS), "task": None, "wake": None}
_pay_checked = {}                   # payment_id -> when it was last checked


def _pay_init():
    with zai_platform._db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS zai_payments (
                id          INTEGER PRIMARY KEY,
                request_id  INTEGER NOT NULL,          -- zai_credit_requests.id
                username    TEXT NOT NULL,
                micros      INTEGER NOT NULL,
                method      TEXT NOT NULL,             -- CARD | SASAI_WALLET | ECOCASH
                reference   TEXT NOT NULL UNIQUE,      -- our merchantReference
                payment_id  TEXT UNIQUE,               -- the provider's paymentId
                status      TEXT NOT NULL,             -- the provider's status, or ABANDONED
                next_action TEXT NOT NULL DEFAULT '',
                redirect    TEXT NOT NULL DEFAULT '',  -- where to send the payer, https only
                created     REAL NOT NULL,
                updated     REAL NOT NULL,
                credited    INTEGER NOT NULL DEFAULT 0,
                ledger_id   INTEGER
            )""")
        con.execute("CREATE INDEX IF NOT EXISTS ix_payments_open ON zai_payments(credited, status)")


def _pay_safe_url(url):
    """`url` if it is https (or http to this machine, for testing), else ''."""
    try:
        u = urlparse(str(url))
    except ValueError:
        return ""
    if u.scheme == "https" and u.netloc:
        return str(url)
    if u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost"):
        return str(url)
    return ""


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------

async def _pay_call(method, path, body=None):
    """(HTTP status, JSON body) from the API; 599 when it cannot be reached.
    The credentials go in headers and are never logged."""
    headers = {"client_id": _PAY["id"], "client_secret": _PAY["secret"],
               "x-correlation-id": str(uuid.uuid4()), "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = HTTPRequest(_PAY["url"] + path, method=method, headers=headers,
                      body=json.dumps(body) if body is not None else None,
                      connect_timeout=5, request_timeout=20)
    try:
        resp = await AsyncHTTPClient().fetch(req, raise_error=False)
    except Exception as e:
        get_logger().warning("payments %s %s: %s", method, path.split("?")[0], type(e).__name__)
        return 599, {}
    try:
        data = json.loads(resp.body) if resp.body else {}
    except ValueError:
        data = {}
    return resp.code, data


def _pay_reason(data):
    """The provider's own words for a refusal, short and single-line, for the log."""
    if not isinstance(data, dict):
        return ""
    words = " ".join(str(data.get(k) or "") for k in ("code", "message")).strip()
    for secret in (_PAY["id"], _PAY["secret"]):
        if secret:
            words = words.replace(secret, "[hidden]")
    return " ".join(words.split())[:300]


def _pay_action(data):
    """(type, https address) of the payer's next step in a payment answer.
    Live the address is nextAction.checkout.url (Sasai's hosted checkout);
    the Postman collection shows nextAction.redirectUrl."""
    action = data.get("nextAction") if isinstance(data, dict) else None
    if not isinstance(action, dict):
        return "", ""
    checkout = action.get("checkout") if isinstance(action.get("checkout"), dict) else {}
    url = checkout.get("url") or action.get("redirectUrl") or action.get("url") or ""
    return str(action.get("type") or ""), _pay_safe_url(url)


def _pay_methods_from(data):
    """[(method, name)] for the known, available methods in a
    /payment-methods answer. Live it is {"methods": [{"method": "CARD",
    "available": true, "displayName": "Bank card"}, ...]}; the Postman
    collection also allows a bare list, "paymentMethods" and "code"."""
    items = data if isinstance(data, list) else (
        (data.get("methods") or data.get("paymentMethods") or data.get("data") or [])
        if isinstance(data, dict) else [])
    out = []
    for m in items if isinstance(items, list) else []:
        name = ""
        if isinstance(m, dict):
            if m.get("available") is False:
                continue
            code = m.get("method") or m.get("paymentMethod") or m.get("code")
            name = " ".join(str(m.get("displayName") or "").split())[:40]
        else:
            code = m
        code = str(code or "").upper()
        if code in _PAY_KNOWN and code not in [c for c, _ in out]:
            out.append((code, name or _PAY_DEFAULT_NAMES[code]))
    return out


async def _pay_refresh_methods():
    code, data = await _pay_call("GET", "/payment-methods?countryCode=ZW")
    found = _pay_methods_from(data) if code == 200 else []
    if found:
        _PAY_LABELS.update(found)
        _PAY["methods"] = [m for m, _ in found]
        zai_credit._CR_ONLINE["methods"] = [(f"online:{m}", name) for m, name in found]
    else:
        get_logger().warning("payments: payment methods answered %s; offering %s", code, ", ".join(_PAY["methods"]))


async def _pay_register_webhook():
    """Ask the provider to call /hub/api/zai/payments/webhook, once."""
    if not _PAY["public"]:
        return
    url = _PAY["public"] + "/hub/api/zai/payments/webhook"
    code, data = await _pay_call("GET", "/webhooks")
    items = data if isinstance(data, list) else (data.get("webhooks") or data.get("data") or [])
    if code == 200 and any(isinstance(w, dict) and w.get("url") == url for w in items):
        return
    code, _ = await _pay_call("POST", "/webhooks", {"url": url, "events": ["payment.succeeded", "payment.failed"]})
    get_logger().info("payments: webhook registration answered %s", code)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

def _pay_close_request(con, rid, status, reason):
    """Close a still-waiting request as declined or cancelled, with why."""
    con.execute("UPDATE zai_credit_requests SET status=?, decided=?, decided_by='eco-payments', reason=?"
                " WHERE id=? AND status='pending'", (status, time.time(), reason[:300], rid))


async def _pay_start(username, email, micros, method, mobile, return_url):
    """Create the request and the payment. Returns where to send the payer
    ('' when there is nowhere to go, e.g. a phone prompt); ValueError with a
    message for the user."""
    method = method.upper()
    if method not in _PAY["methods"]:
        raise ValueError("Choose how you'll pay.")
    mobile = re.sub(r"[\s()-]", "", mobile or "")
    if mobile and not _PAY_MOBILE.match(mobile):
        raise ValueError("Enter your number as +263 followed by 9 digits.")
    if method == "ECOCASH" and not mobile:
        raise ValueError("Enter the EcoCash number to send the payment prompt to.")
    rid = zai_credit._cr_create(username, email, micros, "online", "", _PAY_LABELS[method])
    reference = f"zai-credit-{rid}-{secrets.token_hex(4)}"
    payer = {"customerId": username}
    if email:
        payer["email"] = email
    if mobile:
        payer["mobile"] = "+" + mobile.lstrip("+")
    body = {"merchantReference": reference, "paymentMethod": method,
            "amount": round(micros / zai_platform.MICROS, 2),
            "currency": zai_platform.CATALOG.currency, "description": "ZAI Compute credit",
            "payer": payer}
    # The provider accepts only an https address on our site; without one the
    # payer is not sent back, and the checks below still settle the payment.
    if return_url and urlparse(return_url).scheme == "https":
        body["returnUrl"] = return_url
    code, data = await _pay_call("POST", "/payments", body)
    pid = str(data.get("paymentId") or "") if isinstance(data, dict) else ""
    if code not in (200, 201) or not pid:
        with zai_platform._db() as con:
            _pay_close_request(con, rid, "cancelled", "The payment could not be started.")
        get_logger().warning("payments: create for %s answered %s: %s", username, code, _pay_reason(data))
        raise ValueError("The payment could not be started. Please try again in a minute, "
                         "or choose another way to pay.")
    action_type, redirect = _pay_action(data)
    now = time.time()
    with zai_platform._db() as con:
        con.execute(
            "INSERT INTO zai_payments(request_id, username, micros, method, reference, payment_id, status,"
            " next_action, redirect, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rid, username, micros, method, reference, pid, str(data.get("status") or "PENDING_AUTHORIZATION"),
             action_type, redirect, now, now))
        con.execute("UPDATE zai_credit_requests SET reference=? WHERE id=?", (pid[:100], rid))
    get_logger().info("payments: %s started %s by %s (%s)", username,
                      zai_platform.CATALOG.money(micros), method, pid)
    return redirect


def _pay_apply(payment_id, status, amount=None):
    """Record the provider's status for a payment and act on it. Returns the
    status stored. Crediting happens at most once (credited 0 -> 1). When the
    provider reports the amount and it is not what we asked for, nothing is
    credited: the payment is marked AMOUNT_MISMATCH for an administrator."""
    status = str(status or "").upper()
    now = time.time()
    with zai_platform._db() as con:
        p = con.execute("SELECT * FROM zai_payments WHERE payment_id=?", (payment_id,)).fetchone()
        if p is None:
            return None
        if not status:
            return p["status"]
        if status == "SUCCEEDED" and amount is not None and not p["credited"]:
            try:
                paid = zai_platform.to_micros(f"{float(amount):.6f}")
            except (TypeError, ValueError):
                paid = None
            if paid is None or abs(paid - p["micros"]) >= 5000:       # half a cent
                get_logger().error("payments: %s reported %r paid against %s asked; not credited",
                                   payment_id, amount, zai_platform.CATALOG.money(p["micros"]))
                status = "AMOUNT_MISMATCH"
        con.execute("UPDATE zai_payments SET status=?, updated=? WHERE id=?", (status, now, p["id"]))
        if status == "SUCCEEDED":
            cur = con.execute("UPDATE zai_payments SET credited=1 WHERE id=? AND credited=0", (p["id"],))
            if cur.rowcount == 1:
                note = f"Online payment · {_PAY_LABELS.get(p['method'], p['method'])} · {payment_id}"
                lid = con.execute(
                    "INSERT INTO ledger(username, micros, kind, note, actor, ts) VALUES (?,?,?,?,?,?)",
                    (p["username"], p["micros"], "topup", note[:200], "eco-payments", now)).lastrowid
                con.execute("UPDATE zai_payments SET ledger_id=? WHERE id=?", (lid, p["id"]))
                # Approved even if the user had cancelled: the money arrived.
                con.execute(
                    "UPDATE zai_credit_requests SET status='approved', decided=?, decided_by='eco-payments',"
                    " granted=?, ledger_id=?, reason='' WHERE id=? AND status!='approved'",
                    (now, p["micros"], lid, p["request_id"]))
                get_logger().info("payments: %s credited %s (%s)", p["username"],
                                  zai_platform.CATALOG.money(p["micros"]), payment_id)
        elif status == "FAILED":
            _pay_close_request(con, p["request_id"], "declined", "The payment did not go through. You can try again.")
        elif status == "EXPIRED":
            _pay_close_request(con, p["request_id"], "declined", "The payment expired before it was completed. You can try again.")
        return status


async def _pay_check(payment_id, force=False):
    """Ask the provider about one payment and act on the answer. Checks of
    the same payment closer than _PAY_CHECK_GAP seconds are skipped unless
    `force`. Returns the stored status, or None for an unknown payment."""
    now = time.time()
    if not force and now - _pay_checked.get(payment_id, 0) < _PAY_CHECK_GAP:
        with zai_platform._db() as con:
            p = con.execute("SELECT status FROM zai_payments WHERE payment_id=?", (payment_id,)).fetchone()
        return p["status"] if p else None
    _pay_checked[payment_id] = now
    code, data = await _pay_call("GET", "/payments/" + quote(payment_id, safe=""))
    if code == 200 and isinstance(data, dict) and str(data.get("paymentId") or payment_id) == payment_id:
        # Payments started before the checkout address was read get it now.
        _, redirect = _pay_action(data)
        if redirect:
            with zai_platform._db() as con:
                con.execute("UPDATE zai_payments SET redirect=? WHERE payment_id=? AND redirect=''", (redirect, payment_id))
        return _pay_apply(payment_id, data.get("status"), data.get("amount"))
    if code not in (404, 599):
        get_logger().warning("payments: status of %s answered %s: %s", payment_id, code, _pay_reason(data))
    return _pay_apply(payment_id, "")


async def _pay_sweep():
    """Check every payment still open; give up on ones older than a week."""
    now = time.time()
    with zai_platform._db() as con:
        rows = con.execute(
            "SELECT payment_id, request_id, created FROM zai_payments WHERE credited=0 AND payment_id IS NOT NULL"
            " AND status NOT IN ('FAILED','EXPIRED','REFUNDED','ABANDONED','AMOUNT_MISMATCH')").fetchall()
    for r in rows:
        if now - r["created"] > _PAY_GIVE_UP:
            with zai_platform._db() as con:
                con.execute("UPDATE zai_payments SET status='ABANDONED', updated=? WHERE payment_id=?",
                            (now, r["payment_id"]))
                _pay_close_request(con, r["request_id"], "cancelled", "No payment arrived within seven days.")
            continue
        try:
            await _pay_check(r["payment_id"], force=True)
        except Exception as e:
            get_logger().error("payments: check of %s failed: %s", r["payment_id"], type(e).__name__)


async def _pay_forever():
    _PAY["wake"] = asyncio.Event()
    get_logger().info("payments: on, %s, checked every %gs", _PAY["url"], _PAY["poll"])
    for job in (_pay_refresh_methods, _pay_register_webhook):
        try:
            await job()
        except Exception as e:
            get_logger().error("payments: %s failed: %s", job.__name__, type(e).__name__)
    last_methods = time.time()
    while True:
        try:
            await _pay_sweep()
            if time.time() - last_methods > 3600:
                last_methods = time.time()
                await _pay_refresh_methods()
        except Exception as e:
            get_logger().error("payments: pass failed: %s", type(e).__name__)
        try:
            await asyncio.wait_for(_PAY["wake"].wait(), timeout=_PAY["poll"])
        except asyncio.TimeoutError:
            pass
        _PAY["wake"].clear()


def _pay_ensure_running():
    """Start the background loop once an event loop is running."""
    if _PAY["on"] and _PAY["task"] is None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        _PAY["task"] = asyncio.ensure_future(_pay_forever())


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def _pay_public_base(handler):
    """This site's address for the payer's way back, e.g. https://host."""
    if _PAY["public"]:
        return _PAY["public"]
    return f"{handler.request.protocol}://{handler.request.host}"


async def _pay_start_from_form(handler, micros, method, mobile):
    """zai_credit's hook for an "online:<METHOD>" choice on the credit form."""
    _pay_ensure_running()
    user = handler.current_user
    info = UserInfo.find(handler.db, user.name)
    back = _pay_public_base(handler) + handler.hub.base_url + "credit/pay/return"
    redirect = await _pay_start(user.name, (info.email or "") if info else "", micros, method, mobile, back)
    return redirect or handler.hub.base_url + "account#credit"


def _pay_latest(username):
    """The user's newest payment, as a dict, or None."""
    with zai_platform._db() as con:
        p = con.execute("SELECT * FROM zai_payments WHERE username=? ORDER BY id DESC LIMIT 1",
                        (username,)).fetchone()
    return dict(p) if p else None


class PayReturnHandler(BaseHandler):
    """GET /hub/credit/pay/return -- where the provider sends the payer back."""

    @web.authenticated
    async def get(self):
        p = _pay_latest(self.current_user.name)
        if p and p["payment_id"] and not p["credited"]:
            try:
                await _pay_check(p["payment_id"])
            except Exception as e:
                self.log.error("payments: check on return failed: %s", type(e).__name__)
        self.redirect(self.hub.base_url + "account#credit")


class PayCheckHandler(BaseHandler):
    """POST /hub/credit/pay/check -- check a payment now. A user checks their
    own; an admin may check any (by `payment`). ?format=json answers with the
    status instead of a redirect, for the Account page's script."""

    @web.authenticated
    async def post(self):
        user = self.current_user
        pid = self.get_body_argument("payment", "")
        with zai_platform._db() as con:
            if pid and user.admin:
                p = con.execute("SELECT * FROM zai_payments WHERE payment_id=?", (pid,)).fetchone()
            else:
                p = con.execute("SELECT * FROM zai_payments WHERE username=? ORDER BY id DESC LIMIT 1",
                                (user.name,)).fetchone()
        if p is None or not p["payment_id"]:
            raise web.HTTPError(404, "No such payment.")
        status = await _pay_check(p["payment_id"])
        if self.get_argument("format", "") == "json":
            self.set_header("Content-Type", "application/json")
            return self.finish(json.dumps({"status": status, "credited": status == "SUCCEEDED"}))
        back = "billing#credit-requests" if (pid and user.admin) else "account#credit"
        self.redirect(self.hub.base_url + back)


class PayWebhookHandler(web.RequestHandler):
    """POST /hub/api/zai/payments/webhook -- the provider says a payment
    changed. Unauthenticated, so nothing in it is believed: a known, still
    open payment id only triggers our own GET /payments/{id}. Always 200."""

    def check_xsrf_cookie(self):
        pass

    async def post(self):
        try:
            body = json.loads(self.request.body or b"{}")
        except ValueError:
            body = {}
        pid = ""
        if isinstance(body, dict):
            for part in (body, body.get("data"), body.get("payment")):
                if isinstance(part, dict) and part.get("paymentId"):
                    pid = str(part["paymentId"])[:200]
                    break
        if pid and _PAY["on"]:
            with zai_platform._db() as con:
                open_ = con.execute("SELECT 1 FROM zai_payments WHERE payment_id=? AND credited=0", (pid,)).fetchone()
            if open_:
                asyncio.ensure_future(_pay_check(pid))
        self.set_header("Content-Type", "application/json")
        self.finish("{}")


def _pay_view(user):
    """template_vars: the payment behind the user's newest request, for the
    Account page; and every open payment for an admin's Billing page."""
    if user is None or getattr(user, "kind", "user") != "user":
        return None
    try:
        _pay_ensure_running()
        view = {"on": _PAY["on"], "labels": _PAY_LABELS, "mine": None, "open": {}}
        with zai_platform._db() as con:
            req = con.execute("SELECT id FROM zai_credit_requests WHERE username=? ORDER BY id DESC LIMIT 1",
                              (user.name,)).fetchone()
            if req:
                p = con.execute("SELECT * FROM zai_payments WHERE request_id=? ORDER BY id DESC LIMIT 1",
                                (req["id"],)).fetchone()
                view["mine"] = dict(p) if p else None
            if user.admin:
                for p in con.execute("SELECT * FROM zai_payments WHERE request_id IN"
                                     " (SELECT id FROM zai_credit_requests WHERE status='pending')"):
                    view["open"][p["request_id"]] = dict(p)
        return view
    except Exception as e:
        get_logger().error("payments view: %s", e)
        return None


def configure(c, env=os.environ):
    _PAY.update(on=False, task=None)
    _PAY_LABELS.clear()
    _PAY_LABELS.update(_PAY_DEFAULT_NAMES)
    url = (env.get("ECO_PAYMENTS_URL") or _PAY_DEFAULT_URL).strip().rstrip("/")
    cid = (env.get("ECO_PAYMENTS_CLIENT_ID") or "").strip()
    secret = (env.get("ECO_PAYMENTS_CLIENT_SECRET") or "").strip()
    public = (env.get("ECO_PAYMENTS_PUBLIC_URL") or "").strip().rstrip("/")
    try:
        poll = float(env.get("ECO_PAYMENTS_POLL_SECONDS") or 60)
    except ValueError:
        poll = 60.0
    log = get_logger()
    _pay_init()
    if not (cid and secret):
        log.info("payments are OFF: ECO_PAYMENTS_CLIENT_ID and ECO_PAYMENTS_CLIENT_SECRET are not both set")
    elif not _pay_safe_url(url):
        log.error("payments are OFF: ECO_PAYMENTS_URL must be https")
    else:
        if public and not _pay_safe_url(public):
            log.error("payments: ECO_PAYMENTS_PUBLIC_URL must be https; ignoring it (no webhook)")
            public = ""
        _PAY.update(on=True, url=url, id=cid, secret=secret, public=public,
                    poll=min(max(poll, 15.0), 3600.0), methods=list(_PAY_LABELS))
        zai_credit._CR_ONLINE["start"] = _pay_start_from_form
        zai_credit._CR_ONLINE["methods"] = [(f"online:{m}", _PAY_LABELS[m]) for m in _PAY_LABELS]
    if not _PAY["on"]:
        zai_credit._CR_ONLINE.update(start=None, methods=[])
    handlers = list(c.JupyterHub.get("extra_handlers", []) or [])
    handlers += [
        (r"/credit/pay/return", PayReturnHandler),
        (r"/credit/pay/check", PayCheckHandler),
        (r"/api/zai/payments/webhook", PayWebhookHandler),
    ]
    c.JupyterHub.extra_handlers = handlers
    tv = dict(c.JupyterHub.get("template_vars", {}) or {})
    tv["zai_pay"] = _pay_view
    c.JupyterHub.template_vars = tv
    _pay_ensure_running()
