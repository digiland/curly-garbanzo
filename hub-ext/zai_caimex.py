"""Caimex without a second registration.

A user who signs in to ZAI Compute gets a Caimex account in our organisation
with the same email and password, and an "Open Caimex" link in the nav that
lands them there already signed in. Off unless all five CAIMEX_ settings are
present (docs/CAIMEX.md). Loaded by charts/jupyterhub-values.yaml, 03-zai-platform,
from the hub-zai ConfigMap, like zai_platform.py and zai_2fa.py.

Everything happens at SIGN-IN, not at Approve: registering a Caimex account
needs a password, and sign-in is the only moment the hub holds the user's (it
stores a hash). It is used for the calls below and never written anywhere.

  first sign-in   1. as the service account, invite the user's address into
                     the organisation; the reply carries the invitation's token
                  2. POST /api/auth/register with the user's email, password
                     and that token: the account is created and joins the
                     organisation as an active member
  every sign-in   POST /api/auth/login as the user, and keep the session token
                  Caimex returns (in memory, about a day -- the lifetime of
                  both that token and a hub session)
  /hub/caimex     forwards the browser to Caimex's /auth/callback with the
                  token in the URL fragment, the hand-off its own Google
                  sign-in uses. With no token it offers to sign in again
                  (templates/caimex.html), or sends the user to the Caimex
                  login when that would not help

The service account (a member of the organisation allowed to invite people,
two-factor OFF) is needed because only a member can create invitations, and
Caimex's organisation endpoints take a sign-in session, not an API key.

Signing in never waits on Caimex and never fails because of it. The outcome is
recorded in zai_caimex_accounts (hub database), shown on /hub/authorize, and
anything not done is tried again at the next sign-in. An address that already
has a Caimex account keeps it: the invitation stands for them to accept, and
the hub leaves their password alone.
"""
import asyncio, datetime, json, os, time, urllib.parse

import sqlalchemy as sa
from jupyterhub.handlers import BaseHandler
from nativeauthenticator import NativeAuthenticator
from nativeauthenticator.orm import UserInfo
from tornado import web
from tornado.escape import xhtml_escape
from tornado.httpclient import AsyncHTTPClient, HTTPRequest
from traitlets.log import get_logger

# Read from the environment by configure(); empty until then.
_CX_URL = _CX_WEB = _CX_ORG = _CX_EMAIL = _CX_PASSWORD = ""
_CX_ON = False


def _cx_encrypted(url):
    """https, or plain http to this machine only (local testing)."""
    u = urllib.parse.urlsplit(url)
    return u.scheme == "https" or (
        u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost"))

# Caimex tokens last 24 hours; stop using one a little before that.
_CX_TOKEN_SECONDS = 23 * 3600

# Own MetaData, not JupyterHub's Base: the hub's schema check and
# migrations never see this table.
#   status      created  the hub registered the account
#               invited  the address already had an account; invitation sent
#               failed   see detail; tried again at the next sign-in
#   auto_login  false once Caimex has refused the hub password for this
#               user, so the hub stops knocking on their account
_cx_table = sa.Table(
    "zai_caimex_accounts", sa.MetaData(),
    sa.Column("username", sa.Unicode(255), primary_key=True),
    sa.Column("email", sa.Unicode(320)),
    sa.Column("status", sa.Unicode(32)),
    sa.Column("detail", sa.Unicode(600)),
    sa.Column("auto_login", sa.Boolean),
    sa.Column("updated_at", sa.DateTime))
# db: the hub's one database session (handlers' self.db is the same
# object), captured in get_handlers below; None until then.
_cx = {"db": None, "token": None, "login_failed": None}
_cx_sessions = {}                 # username -> (Caimex token, issued at)
_cx_running = {}                  # username -> the sign-in task in flight

# These two share the hub's session, as NativeAuthenticator's own writes
# do, so the commit below also commits whatever the hub had pending. A
# second connection would be cleaner but can stall the whole hub: on
# SQLite it waits, on the event loop, for a lock the hub itself holds.
# The session is rolled back here, and only here, when one of OUR
# statements fails -- it is unusable until then. Nothing else in this
# file rolls back, so a Caimex or network error never touches hub data.
def _cx_get(db, username):
    try:
        row = db.execute(
            sa.select(_cx_table.c.status, _cx_table.c.detail, _cx_table.c.auto_login)
            .where(_cx_table.c.username == username)).first()
    except Exception:
        db.rollback()
        raise
    return (row[0], row[1] or "", row[2] is not False) if row else (None, "", True)

def _cx_set(db, username, email, status, detail="", auto_login=True):
    try:
        db.execute(_cx_table.delete().where(_cx_table.c.username == username))
        db.execute(_cx_table.insert().values(
            username=username, email=email, status=status, detail=detail[:600],
            auto_login=auto_login,
            updated_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)))
        db.commit()
    except Exception:
        db.rollback()
        raise

async def _cx_fetch(path, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = HTTPRequest(
        _CX_URL + path, method="POST", headers=headers,
        body=json.dumps(body), connect_timeout=5, request_timeout=20)
    # raise_error=False covers HTTP statuses only; a refused connection,
    # DNS failure or timeout still raises. Report those as 599 too.
    try:
        resp = await AsyncHTTPClient().fetch(req, raise_error=False)
    except Exception as e:
        get_logger().warning("caimex %s: %s", path, e)
        return 599, {}
    try:
        data = json.loads(resp.body) if resp.body else {}
    except ValueError:
        data = {}
    return resp.code, data if isinstance(data, dict) else {}

def _cx_reason(code, data):
    if code == 599:
        return "Caimex could not be reached"
    # Caimex's wording only, never the body. Sign-in and register replies
    # carry tokens, and its validation errors (422) repeat the rejected
    # value under "input" -- for a register call, the user's password.
    detail = data.get("detail", "")
    if isinstance(detail, list):
        detail = "; ".join(
            ".".join(str(p) for p in e.get("loc", [])[1:]) + ": " + str(e.get("msg", ""))
            for e in detail if isinstance(e, dict))
    elif not isinstance(detail, str):
        detail = ""
    return f"HTTP {code} {detail}".strip()[:280]

async def _cx_service_login():
    """Sign in as the service account and cache the token. Returns None,
    or (code, data) to report as the failure. Caimex throttles sign-in
    attempts per address, so a refusal is not retried for five minutes."""
    failed = _cx["login_failed"]
    if failed and time.monotonic() - failed[0] < 300:
        return failed[1]
    code, data = await _cx_fetch(
        "/api/auth/login", {"email": _CX_EMAIL, "password": _CX_PASSWORD})
    if code == 200 and data.get("access_token"):
        _cx["token"], _cx["login_failed"] = data["access_token"], None
        return None
    if code == 599:
        return 599, {}
    if code == 200 and data.get("mfa_required"):
        why = "the Caimex service account has two-factor on; turn it off"
    else:
        why = "Caimex service sign-in refused: " + _cx_reason(code, data)
    _cx["login_failed"] = (time.monotonic(), (401, {"detail": why}))
    return _cx["login_failed"][1]

async def _cx_invite(email):
    """Invite email into the organization as the service account, signing
    in first if needed and once more if the cached token has expired.
    Re-inviting a pending address returns the same invitation."""
    for attempt in (1, 2):
        if not _cx["token"]:
            failure = await _cx_service_login()
            if failure:
                return failure
        code, data = await _cx_fetch(
            f"/api/organizations/{_CX_ORG}/invitations",
            {"email": email, "role": "member"}, _cx["token"])
        if code != 401 or attempt == 2:
            return code, data
        _cx["token"] = None

def _cx_keep(username, token):
    _cx_sessions[username] = (token, time.monotonic())

def _cx_session(username):
    token, at = _cx_sessions.get(username, (None, 0))
    if token and time.monotonic() - at < _CX_TOKEN_SECONDS:
        return token
    _cx_sessions.pop(username, None)
    return None

async def _cx_register(db, username, email, password):
    """First sign-in: create the account inside the organization.
    Returns True when the user is now signed in to Caimex."""
    code, data = await _cx_invite(email)
    # 409: this address is already a member, so it already has an account.
    if code not in (201, 409):
        why = "invitation: " + _cx_reason(code, data)
        get_logger().error("caimex account for %s: %s", username, why)
        _cx_set(db, username, email, "failed", why)
        return False
    if code == 201:
        # full_name: the hub has no real name, only the username.
        # Without one Caimex shows the bare email address.
        reg, rdata = await _cx_fetch("/api/auth/register", {
            "email": email, "password": password, "full_name": username,
            "invite_token": data.get("invite_token")})
        if reg == 201 and rdata.get("access_token"):
            _cx_set(db, username, email, "created")
            _cx_keep(username, rdata["access_token"])
            return True
        if reg != 409:          # 409: the address already has an account
            why = "register: " + _cx_reason(reg, rdata)
            get_logger().error("caimex account for %s: %s", username, why)
            _cx_set(db, username, email, "failed", why)
            return False
    # Their own account, with their own password. The invitation (just
    # sent, unless they were a member already) is theirs to accept.
    _cx_set(db, username, email, "invited")
    return False

async def _cx_sign_in(username, password):
    """Run after every successful hub sign-in. Never raises."""
    db = _cx["db"]
    if db is None:
        return
    email = ""
    try:
        info = UserInfo.find(db, username)
        if info is None or not info.is_authorized:
            return
        email = (info.email or "").strip().lower()
        status, _, auto_login = _cx_get(db, username)
        if status not in ("created", "invited"):
            if not email:
                _cx_set(db, username, "", "failed", "No email address on file.")
                return
            if await _cx_register(db, username, email, password):
                return
            status, _, auto_login = _cx_get(db, username)
        if status == "failed" or not auto_login or _cx_session(username):
            return
        code, data = await _cx_fetch(
            "/api/auth/login", {"email": email, "password": password})
        if code == 200 and data.get("access_token"):
            _cx_keep(username, data["access_token"])
        elif code in (200, 401):
            # Two-factor on their Caimex account, or a different password
            # there. Either is the user's choice: Open Caimex shows them
            # the Caimex login from now on, and the hub stops trying.
            _cx_set(db, username, email, status, auto_login=False)
    except Exception as e:
        # The type only: an exception's text is not ours to vouch for.
        get_logger().error("caimex account for %s: %s", username, type(e).__name__)
        try:
            if _cx_get(db, username)[0] not in ("created", "invited"):
                _cx_set(db, username, email, "failed",
                        "Internal error: " + type(e).__name__)
        except Exception:
            pass


_na_authenticate = None          # the original, captured by configure()


async def _authenticate(self, handler, data):
    username = await _na_authenticate(self, handler, data)
    # One at a time per user; a second sign-in during the first adds nothing.
    if username and _CX_ON and username not in _cx_running:
        task = asyncio.ensure_future(_cx_sign_in(username, data["password"]))
        _cx_running[username] = task    # the loop only holds a weak reference
        task.add_done_callback(lambda _t, name=username: _cx_running.pop(name, None))
    return username


class OpenCaimex(BaseHandler):
    """Send the signed-in user to Caimex, signed in there if we can."""

    @web.authenticated
    async def get(self):
        if not _CX_ON:
            raise web.HTTPError(404)
        name = self.current_user.name
        landing = f"/org/{_CX_ORG}/overview"
        # Clicked seconds after signing in: the Caimex work may still be
        # running. Wait for it rather than fall through to a login page.
        task = _cx_running.get(name)
        if task is not None:
            await asyncio.wait([task], timeout=15)
        token = _cx_session(name)
        self.set_header("Cache-Control", "no-store")
        self.set_header("Referrer-Policy", "no-referrer")
        if token:
            # In the fragment, which browsers never send to a server.
            # Sent as a page that forwards the browser, NOT as a
            # redirect: the hub logs every redirect's target, and this
            # one is a live session.
            url = f"{_CX_WEB}/auth/callback#" + urllib.parse.urlencode(
                {"token": token, "redirect": landing})
            self.set_header("Content-Type", "text/html; charset=utf-8")
            self.finish(
                '<!doctype html><meta charset="utf-8">'
                '<meta name="referrer" content="no-referrer">'
                f'<meta http-equiv="refresh" content="0;url={xhtml_escape(url)}">'
                "<title>Opening Caimex</title><p>Opening Caimex&hellip;</p>")
            return
        # No session. The hub only gets one when a password is typed, so
        # a hub restart or a day-old tab leaves a signed-in user without
        # it. If typing the password again would fix that, say so, once;
        # otherwise (their own Caimex password or two-factor, or we just
        # tried) Caimex's own login is the right place.
        status, _, auto_login = _cx_get(self.db, name)
        if (status in ("created", "invited") and auto_login
                and not self.get_argument("tried", "")):
            html = await self.render_template(
                "caimex.html",
                again_url=self.hub.base_url + "caimex/again",
                caimex_url=_CX_WEB + landing)
            self.finish(html)
        else:
            self.redirect(_CX_WEB + landing)


class OpenCaimexAgain(BaseHandler):
    """Sign out of the hub and come straight back through the login page
    to Open Caimex, which then has a fresh session to hand over."""

    @web.authenticated
    async def get(self):
        self.clear_login_cookie()
        self.redirect(self.hub.base_url + "login?" + urllib.parse.urlencode(
            {"next": self.hub.base_url + "caimex?tried=1"}))


_na_get_handlers_cx = None       # the original, captured by configure()


def _get_handlers_cx(self, app):
    handlers = list(_na_get_handlers_cx(self, app))
    if _CX_ON:
        try:
            _cx_table.create(app.db.get_bind(), checkfirst=True)
            _cx["db"] = app.db
        except Exception as e:
            self.log.error("caimex accounts disabled, no status table: %s", e)
    return handlers + [(r"/caimex", OpenCaimex), (r"/caimex/again", OpenCaimexAgain)]


def _cx_status(username):
    """(status, detail) for the Approve page; status None = not tried yet."""
    try:
        return _cx_get(_cx["db"], username)[:2]
    except Exception:
        return (None, "")


def configure(c):
    global _CX_URL, _CX_WEB, _CX_ORG, _CX_EMAIL, _CX_PASSWORD, _CX_ON
    global _na_authenticate, _na_get_handlers_cx
    _CX_URL = os.environ.get("CAIMEX_API_URL", "").strip().rstrip("/")
    _CX_WEB = os.environ.get("CAIMEX_WEB_URL", "").strip().rstrip("/")
    _CX_ORG = os.environ.get("CAIMEX_ORG_ID", "").strip()
    _CX_EMAIL = os.environ.get("CAIMEX_SERVICE_EMAIL", "").strip()
    _CX_PASSWORD = os.environ.get("CAIMEX_SERVICE_PASSWORD", "")
    settings = {
        "CAIMEX_API_URL": _CX_URL, "CAIMEX_WEB_URL": _CX_WEB, "CAIMEX_ORG_ID": _CX_ORG,
        "CAIMEX_SERVICE_EMAIL": _CX_EMAIL, "CAIMEX_SERVICE_PASSWORD": _CX_PASSWORD}
    _CX_ON = all(settings.values())
    if any(settings.values()) and not _CX_ON:
        get_logger().warning(
            "caimex accounts are OFF: not set: %s",
            ", ".join(k for k, v in settings.items() if not v))
    elif _CX_ON and not (_cx_encrypted(_CX_URL) and _cx_encrypted(_CX_WEB)):
        # Users' passwords and sessions travel to these addresses.
        get_logger().error(
            "caimex accounts are OFF: CAIMEX_API_URL and CAIMEX_WEB_URL must be https")
        _CX_ON = False

    # Wrap the authenticator once, however many times configure() runs.
    if _na_authenticate is None:
        _na_authenticate = NativeAuthenticator.authenticate
        _na_get_handlers_cx = NativeAuthenticator.get_handlers
        NativeAuthenticator.authenticate = _authenticate
        NativeAuthenticator.get_handlers = _get_handlers_cx

    # JupyterHub CALLS any callable in template_vars with the current user and
    # hands the template the result, so a function meant for the template to
    # call has to be returned from one.
    tv = dict(c.JupyterHub.get("template_vars", {}) or {})
    tv["caimex_on"] = _CX_ON
    tv["caimex_status"] = lambda user: _cx_status
    c.JupyterHub.template_vars = tv
