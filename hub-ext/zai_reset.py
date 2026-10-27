"""Self-service password reset by email.

/hub/forgot asks for an email address and, if an approved account has it,
emails a one-time link to /hub/reset/<link>. The link works once, for 30
minutes; there the user sets a new password (the signup rules apply) and, if
they use two-factor sign-in, enters a current authenticator code. Changing
the password signs every other device out (the user's cookie_id is renewed),
clears the sign-in lockout, and sends a "your password was changed" email.
See docs/PASSWORD-RESET.md. Loaded by charts/jupyterhub-values.yaml,
03-zai-platform, after zai_2fa (whose code check it uses).

Off unless an email sender and the site's public address are configured:
RESEND_API_KEY (Resend) or SMTP_HOST (any mailbox), a from-address
(MAIL_FROM, else APPROVAL_FROM_EMAIL), and ZAI_PUBLIC_URL. The link is built
from ZAI_PUBLIC_URL, never from the request: a forged Host header could
otherwise send a user a link to someone else's site.

What the page says never depends on whether the address has an account, so
it cannot be used to find out who is a customer. The database keeps only a
SHA-256 of each link; the link itself exists only in the email. Neither the
link nor a password is ever logged.
"""
import asyncio
import hashlib
import json
import os
import re
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage
from urllib.parse import urlparse

import sqlalchemy as sa
import jupyterhub.log
from jupyterhub import orm
from jupyterhub.handlers.base import BaseHandler
from jupyterhub.utils import new_token
from nativeauthenticator.orm import UserInfo
from tornado.httpclient import AsyncHTTPClient, HTTPRequest
from traitlets.log import get_logger

import zai_2fa

_RS_LIFETIME = 30 * 60              # a link works for 30 minutes
_RS_PER_EMAIL = 3                   # links per email address per hour
_RS_PER_IP = 20                     # requests per visitor per hour (an office may share one address)
_RS_CODE_TRIES = 5                  # wrong authenticator codes before a link dies
_RS_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s.]{2,}$")

_RS = {"on": False, "public": "", "from": "", "sender": None, "smtp": {}, "resend": ""}
_rs_recent = {}                     # rate-limit key -> [request times]

_rs_table = sa.Table(
    "zai_password_resets", sa.MetaData(),
    sa.Column("link_hash", sa.Unicode(64), primary_key=True),
    sa.Column("username", sa.Unicode(255), nullable=False, index=True),
    sa.Column("created", sa.Float, nullable=False),
    sa.Column("expires", sa.Float, nullable=False),
    sa.Column("used", sa.Float),                     # when it was used or cancelled
    sa.Column("bad_codes", sa.Integer, nullable=False, default=0))
_rs_ready = set()                   # engines the table is known to exist on


def _rs_db(db):
    bind = db.get_bind()
    if id(bind) not in _rs_ready:
        _rs_table.create(bind, checkfirst=True)
        _rs_ready.add(id(bind))
    return db


def _rs_hash(link):
    return hashlib.sha256(link.encode()).hexdigest()


def _rs_allowed(key, limit, now):
    """True and counted if `key` has had fewer than `limit` requests this hour."""
    times = [t for t in _rs_recent.get(key, []) if now - t < 3600]
    if len(times) >= limit:
        _rs_recent[key] = times
        return False
    _rs_recent[key] = times + [now]
    return True


# ---------------------------------------------------------------------------
# The steps, free of web concerns so they can be tested directly
# ---------------------------------------------------------------------------

def _rs_request(db, email, visitor, now=None):
    """Make reset links for the approved accounts with this email address.
    Returns [(username, email, link)] to send; usually one, and empty when
    there is no such account or a limit was reached. Never raises for an
    unknown address: the caller says the same thing either way."""
    now = now or time.time()
    email = (email or "").strip().lower()
    if not _RS_EMAIL.match(email):
        return []
    if not _rs_allowed("ip:" + visitor, _RS_PER_IP, now) or not _rs_allowed("email:" + email, _RS_PER_EMAIL, now):
        get_logger().warning("password reset: request limit reached")
        return []
    _rs_db(db)
    out = []
    try:
        for info in db.query(UserInfo).filter(sa.func.lower(UserInfo.email) == email).limit(3):
            if not info.is_authorized:
                continue
            # A new link cancels any earlier one still unused.
            db.execute(_rs_table.update().where(_rs_table.c.username == info.username)
                       .where(_rs_table.c.used.is_(None)).values(used=now))
            link = secrets.token_urlsafe(32)
            db.execute(_rs_table.insert().values(link_hash=_rs_hash(link), username=info.username,
                                                 created=now, expires=now + _RS_LIFETIME, used=None, bad_codes=0))
            out.append((info.username, info.email, link))
        db.commit()
    except Exception:
        db.rollback()
        raise
    for username, _, _ in out:
        get_logger().info("password reset: link sent to %s", username)
    return out


def _rs_find(db, link, now=None):
    """The username a link is for, or None if it is unknown, used or expired."""
    now = now or time.time()
    _rs_db(db)
    row = db.execute(sa.select(_rs_table).where(_rs_table.c.link_hash == _rs_hash(link or ""))).first()
    if row is None or row.used is not None or row.expires < now:
        return None
    return row.username


def _rs_complete(db, authenticator, link, password, confirm, code, now=None):
    """Set the new password. Returns the username; ValueError with a message
    for the user. The link is spent only on success, or after too many wrong
    authenticator codes."""
    now = now or time.time()
    username = _rs_find(db, link, now)
    if username is None:
        raise ValueError("This link has expired or was already used. Ask for a new one.")
    info = UserInfo.find(db, username)
    if info is None or not info.is_authorized:
        raise ValueError("This link has expired or was already used. Ask for a new one.")
    if password != confirm:
        raise ValueError("The two passwords don't match.")
    if not authenticator.is_password_strong(password):
        raise ValueError(f"Choose a password of at least {authenticator.minimum_password_length} characters "
                         "that isn't a common one.")
    h = _rs_hash(link)
    if info.has_2fa and not zai_2fa.verify(info, code):
        db.execute(_rs_table.update().where(_rs_table.c.link_hash == h)
                   .values(bad_codes=_rs_table.c.bad_codes + 1))
        bad = db.execute(sa.select(_rs_table.c.bad_codes).where(_rs_table.c.link_hash == h)).scalar() or 0
        if bad >= _RS_CODE_TRIES:
            db.execute(_rs_table.update().where(_rs_table.c.link_hash == h).values(used=now))
            db.commit()
            get_logger().warning("password reset: link for %s cancelled after %d wrong codes", username, bad)
            raise ValueError("Too many wrong codes. This link no longer works; ask for a new one.")
        db.commit()
        raise ValueError("That authenticator code didn't match. Enter the code your app shows now.")
    # Spend the link first, and only if nobody else just did.
    spent = db.execute(_rs_table.update().where(_rs_table.c.link_hash == h)
                       .where(_rs_table.c.used.is_(None)).values(used=now))
    if spent.rowcount != 1:
        db.rollback()
        raise ValueError("This link has expired or was already used. Ask for a new one.")
    db.execute(_rs_table.update().where(_rs_table.c.username == username)
               .where(_rs_table.c.used.is_(None)).values(used=now))
    db.commit()
    if not authenticator.change_password(username, password):
        raise ValueError("The password could not be changed. Ask for a new link.")
    # Sign every device out, and lift any lockout from failed sign-ins.
    user = orm.User.find(db, username)
    if user is not None:
        user.cookie_id = new_token()
        db.commit()
    authenticator.successful_login(username)
    get_logger().info("password reset: %s changed their password", username)
    return username


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _rs_reset_email(username, link):
    url = f"{_RS['public']}/hub/reset/{link}"
    return ("Reset your ZAI Compute password",
            f"Hello {username},\n\n"
            "Someone, hopefully you, asked to reset the password for your ZAI Compute account.\n\n"
            f"Reset it here. The link works once, for {_RS_LIFETIME // 60} minutes:\n{url}\n\n"
            "If you didn't ask, ignore this email: your password stays as it is.\n\n"
            "ZAI Compute\n")


def _rs_changed_email(username, when):
    stamp = time.strftime("%d %b %Y %H:%M UTC", time.gmtime(when))
    return ("Your ZAI Compute password was changed",
            f"Hello {username},\n\n"
            f"The password for your ZAI Compute account was changed on {stamp}, and every device "
            "that was signed in has been signed out.\n\n"
            "If you did this, there is nothing more to do. Your Caimex password has not changed.\n\n"
            "If you didn't, contact ZAI support straight away.\n\n"
            "ZAI Compute\n")


async def _rs_send_resend(to, subject, text):
    req = HTTPRequest("https://api.resend.com/emails", method="POST", request_timeout=20,
                      headers={"Authorization": f"Bearer {_RS['resend']}", "Content-Type": "application/json"},
                      body=json.dumps({"from": _RS["from"], "to": [to], "subject": subject, "text": text}))
    resp = await AsyncHTTPClient().fetch(req, raise_error=False)
    if resp.code >= 300:
        raise RuntimeError(f"Resend answered {resp.code}")


def _rs_send_smtp_now(to, subject, text):
    s = _RS["smtp"]
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = _RS["from"], to, subject
    msg.set_content(text)
    if s["security"] == "ssl":
        server = smtplib.SMTP_SSL(s["host"], s["port"], timeout=20, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(s["host"], s["port"], timeout=20)
    with server:
        if s["security"] == "starttls":
            server.starttls(context=ssl.create_default_context())
        if s["username"]:
            server.login(s["username"], s["password"])
        server.send_message(msg)


async def _rs_send_smtp(to, subject, text):
    await asyncio.get_running_loop().run_in_executor(None, _rs_send_smtp_now, to, subject, text)


async def _rs_send(to, subject, text):
    """Send one email; failures are logged, never shown (that would say the
    address has an account)."""
    try:
        await _RS["sender"](to, subject, text)
    except Exception as e:
        get_logger().error("password reset: email could not be sent: %s", type(e).__name__)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

class ForgotHandler(BaseHandler):
    """GET/POST /hub/forgot -- ask for a reset link."""

    async def get(self):
        if not _RS["on"]:
            return self.redirect(self.hub.base_url + "login")
        self.finish(await self.render_template("forgot.html", sent=False, email=""))

    async def post(self):
        if not _RS["on"]:
            return self.redirect(self.hub.base_url + "login")
        email = self.get_body_argument("email", "")[:320]
        for username, to, link in _rs_request(self.db, email, self.request.remote_ip or ""):
            subject, text = _rs_reset_email(username, link)
            asyncio.ensure_future(_rs_send(to, subject, text))
        # The same page whether or not the address has an account.
        self.finish(await self.render_template("forgot.html", sent=True, email=email.strip()))


class ResetHandler(BaseHandler):
    """GET/POST /hub/reset/<link> -- choose a new password."""

    async def _page(self, link, username, error="", done=False):
        info = UserInfo.find(self.db, username) if username else None
        self.finish(await self.render_template(
            "reset.html", link=link, username=username, error=error, done=done,
            needs_code=bool(info and info.has_2fa),
            min_length=self.authenticator.minimum_password_length))

    async def get(self, link):
        if not _RS["on"]:
            return self.redirect(self.hub.base_url + "login")
        await self._page(link, _rs_find(self.db, link))

    async def post(self, link):
        if not _RS["on"]:
            return self.redirect(self.hub.base_url + "login")
        try:
            username = _rs_complete(self.db, self.authenticator, link,
                                    self.get_body_argument("password", ""), self.get_body_argument("confirm", ""),
                                    self.get_body_argument("code", ""))
        except ValueError as e:
            return await self._page(link, _rs_find(self.db, link), error=str(e))
        info = UserInfo.find(self.db, username)
        if info is not None and info.email:
            subject, text = _rs_changed_email(username, time.time())
            asyncio.ensure_future(_rs_send(info.email, subject, text))
        self.clear_login_cookie()
        await self._page(link, username, done=True)


def _rs_safe_public(url):
    u = urlparse(url)
    if u.scheme == "https" and u.netloc:
        return url
    if u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost"):
        return url
    return ""


_RS_LINK_IN_PATH = re.compile(r"(/reset/)[A-Za-z0-9_-]+")


def _rs_hide_links_in_request_log():
    """JupyterHub logs every request's address, and a reset link is one. It
    already hides its own secrets with jupyterhub.log._scrub_uri; extend that
    so /hub/reset/<link> is logged as /hub/reset/[secret]."""
    original = jupyterhub.log._scrub_uri
    if getattr(original, "_zai_reset", False):
        return

    def scrub(uri):
        return original(_RS_LINK_IN_PATH.sub(r"\1[secret]", uri))
    scrub._zai_reset = True
    jupyterhub.log._scrub_uri = scrub


def configure(c, env=os.environ):
    log = get_logger()
    _rs_hide_links_in_request_log()
    public = _rs_safe_public((env.get("ZAI_PUBLIC_URL") or "").strip().rstrip("/"))
    sender = (env.get("MAIL_FROM") or env.get("APPROVAL_FROM_EMAIL") or "").strip()
    resend = (env.get("RESEND_API_KEY") or "").strip()
    host = (env.get("SMTP_HOST") or "").strip()
    security = (env.get("SMTP_SECURITY") or "starttls").strip().lower()
    try:
        port = int(env.get("SMTP_PORT") or (465 if security == "ssl" else 587))
    except ValueError:
        port = 587
    _RS.update(on=False, public=public, sender=None)
    why = ""
    if not public:
        why = "ZAI_PUBLIC_URL is not set or not https"
    elif not sender:
        why = "no from-address (MAIL_FROM or APPROVAL_FROM_EMAIL)"
    elif resend:
        _RS.update(on=True, sender=_rs_send_resend, resend=resend, **{"from": sender})
    elif host:
        if security not in ("starttls", "ssl", "none"):
            why = "SMTP_SECURITY must be starttls, ssl or none"
        elif security == "none" and host not in ("127.0.0.1", "localhost"):
            why = "SMTP_SECURITY none is only allowed for a mail server on this machine"
        else:
            _RS.update(on=True, sender=_rs_send_smtp, **{"from": sender}, smtp={
                "host": host, "port": port, "security": security,
                "username": (env.get("SMTP_USERNAME") or "").strip(), "password": env.get("SMTP_PASSWORD") or ""})
    else:
        why = "no email sender (RESEND_API_KEY or SMTP_HOST)"
    if _RS["on"]:
        log.info("password reset: on, links to %s, sent by %s", public, "Resend" if resend else f"SMTP {host}:{port}")
    else:
        log.info("password reset is OFF: %s", why)
    handlers = list(c.JupyterHub.get("extra_handlers", []) or [])
    handlers += [(r"/forgot", ForgotHandler), (r"/reset/([A-Za-z0-9_-]{20,100})", ResetHandler)]
    c.JupyterHub.extra_handlers = handlers
    tv = dict(c.JupyterHub.get("template_vars", {}) or {})
    tv["zai_reset_on"] = lambda user: _RS["on"]
    c.JupyterHub.template_vars = tv
