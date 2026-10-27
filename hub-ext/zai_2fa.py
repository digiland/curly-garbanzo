"""Optional two-factor sign-in with an authenticator app (TOTP).

NativeAuthenticator already stores a per-user `otp_secret` and `has_2fa` and,
for a user with has_2fa set, checks the `2fa` login field. What it lacks is a
way to turn it on: only a checkbox at signup, the secret shown once as text,
and nothing for existing accounts. This adds:

- /hub/account/2fa: scan a QR code, confirm one code to turn it on; password
  plus a code to turn it off
- an admin reset (lost phone) from the Approve users page
- a stricter code check than onetimepass's: one step of clock drift either
  way, and a code that was accepted once is not accepted again
- sign-in in two steps: the sign-in page asks only for the username and
  password, and a user with 2FA on is then asked for a code on its own page,
  /hub/login/code

API tokens are unaffected: 2FA guards the browser sign-in, not tokens a user
already made.

Loaded by extraConfig `03-zai-platform` next to zai_platform.py.
"""

import base64
import hashlib
import hmac
import io
import os
import secrets
import struct
import time
from urllib.parse import quote

from jupyterhub.handlers.base import BaseHandler
from nativeauthenticator import NativeAuthenticator
from nativeauthenticator.handlers import LoginHandler
from nativeauthenticator.orm import UserInfo
from tornado import web
from tornado.escape import url_escape

ISSUER = os.environ.get("ZAI_2FA_ISSUER", "ZAI Notebooks")
STEP = 30

# username -> last accepted time step. In memory: the hub is one process, and
# after a restart the window a code could be replayed in is 90 s at most.
_last_used = {}


def new_secret():
    # 16 base32 characters (80 bits): what NativeAuthenticator generates, and
    # what fits its otp_secret column.
    return base64.b32encode(os.urandom(10)).decode()


def _code(key, counter):
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    return "%06d" % ((struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000)


def verify(info, code, now=None):
    """True if `code` is the current code for `info` (a UserInfo), allowing
    one step of drift. Each step is accepted once."""
    code = (code or "").replace(" ", "").strip()
    if len(code) != 6 or not code.isdigit() or not info.otp_secret:
        return False
    secret = info.otp_secret.upper()
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    t = int((now or time.time()) // STEP)
    for counter in (t - 1, t, t + 1):
        if hmac.compare_digest(_code(key, counter), code):
            if counter <= _last_used.get(info.username, -1):
                return False
            _last_used[info.username] = counter
            return True
    return False


def otpauth_uri(username, secret):
    label = quote(f"{ISSUER}:{username}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&algorithm=SHA1&digits=6&period={STEP}"


def qr_svg(text):
    """Inline SVG of a QR code, or "" when the qrcode package is missing (the
    page then shows only the key to type in)."""
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return ""
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.index("<svg"):]


def enabled(db, username):
    info = UserInfo.find(db, username)
    return bool(info and info.has_2fa)


class TwoFactorHandler(BaseHandler):
    """GET/POST /hub/account/2fa"""

    async def _page(self, info, setup=False, message="", error=""):
        secret = info.otp_secret if (info and setup) else ""
        html = await self.render_template(
            "two-factor.html", info=info, setup=setup, secret=secret,
            spaced=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
            qr=qr_svg(otpauth_uri(info.username, secret)) if secret else "",
            message=message, error=error)
        self.finish(html)

    @web.authenticated
    async def get(self):
        await self._page(UserInfo.find(self.db, self.current_user.name))

    @web.authenticated
    async def post(self):
        user = self.current_user
        action = self.get_body_argument("action", "")
        if action == "reset":
            return await self._reset(user)
        info = UserInfo.find(self.db, user.name)
        if info is None:
            raise web.HTTPError(400, "This account has no password sign-in, so it has no two-factor setting.")
        code = self.get_body_argument("code", "")
        if action == "start" and not info.has_2fa:
            info.otp_secret = new_secret()
            self.db.commit()
            return await self._page(info, setup=True)
        if action == "enable" and not info.has_2fa:
            if not verify(info, code):
                return await self._page(info, setup=True, error=(
                    "That code didn't match. Check the phone's clock is set automatically, "
                    "then enter the code currently shown."))
            info.has_2fa = True
            self.db.commit()
            self.log.info("2fa: %s turned it on", user.name)
            return await self._page(info, message=(
                "Two-factor sign-in is on. From now on, sign-in asks for a code from your app."))
        if action == "disable" and info.has_2fa:
            auth = self.authenticator
            if auth.is_blocked(user.name):
                return await self._page(info, error="Too many failed attempts. Try again in 15 minutes.")
            if not (info.is_valid_password(self.get_body_argument("password", "")) and verify(info, code)):
                auth.add_login_attempt(user.name)
                return await self._page(info, error="The password or the code is wrong.")
            auth.successful_login(user.name)
            info.has_2fa = False
            info.otp_secret = new_secret()      # a later setup must not reuse the old app entry
            self.db.commit()
            self.log.info("2fa: %s turned it off", user.name)
            return await self._page(info, message="Two-factor sign-in is off. You can delete the entry in your app.")
        await self._page(info)

    async def _reset(self, user):
        """Admin: turn 2FA off for someone who lost their phone. Redirects back
        to the Approve users page."""
        if not user.admin:
            raise web.HTTPError(403, "Administrators only")
        name = self.get_body_argument("username", "")
        info = UserInfo.find(self.db, name)
        if info is None:
            raise web.HTTPError(404, "No such user")
        info.has_2fa = False
        info.otp_secret = new_secret()
        _last_used.pop(name, None)
        self.db.commit()
        self.log.warning("2fa: %s reset two-factor for %s", user.name, name)
        self.redirect(f"{self.hub.base_url}authorize#{name}")


# -- sign-in in two steps ----------------------------------------------------
# A user whose password is right and who has 2FA on gets a sign-in waiting
# for its code, kept here, in memory only, and a cookie naming it. The
# password is kept with it because signing in again with the code runs every
# check from the start, and zai_caimex signs in to Caimex with it. It is gone
# once the code is accepted, after _SL_LIFETIME, after _SL_TRIES wrong codes,
# or when the user goes back.
_SL_LIFETIME = 5 * 60
_SL_TRIES = 5
_SL_COOKIE = "zai-signin-code"
_SL_TIMED_OUT = "Sign-in timed out. Sign in again."
_SL_TOO_MANY = "Too many wrong codes. Sign in again."
_SL_LOCKED = "Sign-in is locked for 15 minutes after 5 failed attempts."

# sha256(cookie) -> {"username", "password", "expires", "tries"}
_sl_waiting = {}


def _sl_key(ticket):
    return hashlib.sha256((ticket or "").encode()).hexdigest()


def _sl_start(username, password, now=None):
    """A sign-in waiting for its code; returns the cookie value. A newer one
    for the same user replaces the older."""
    now = now or time.time()
    for key, w in list(_sl_waiting.items()):
        if w["expires"] <= now or w["username"] == username:
            del _sl_waiting[key]
    ticket = secrets.token_urlsafe(32)
    _sl_waiting[_sl_key(ticket)] = {"username": username, "password": password,
                                    "expires": now + _SL_LIFETIME, "tries": 0}
    return ticket


def _sl_find(ticket, now=None):
    w = _sl_waiting.get(_sl_key(ticket))
    if w and w["expires"] <= (now or time.time()):
        _sl_drop(ticket)
        return None
    return w


def _sl_drop(ticket):
    _sl_waiting.pop(_sl_key(ticket), None)


def _sl_needs_code(auth, data):
    """The username if this is the first step for a user with 2FA on: right
    password, approved, not locked out, and no code sent. Anything else is
    left to NativeAuthenticator, so its answer to a wrong password stays the
    same for everyone and does not show who has 2FA on."""
    if (data.get("2fa") or "").strip():
        return None
    username = auth.normalize_username(data.get("username") or "")
    info = auth.get_user(username) if username else None
    if not (info and info.has_2fa and info.is_authorized):
        return None
    if auth.allowed_failed_logins and auth.is_blocked(username):
        return None
    if not info.is_valid_password(data.get("password") or ""):
        return None
    return username


class TwoStepLoginHandler(LoginHandler):
    """POST /hub/login: NativeAuthenticator's sign-in, except that a user with
    2FA on is sent on to /hub/login/code instead of needing the code here."""

    async def post(self):
        data = {arg: self.get_argument(arg, strip=False) for arg in self.request.arguments}
        username = _sl_needs_code(self.authenticator, data)
        if username is None:
            return await super().post()
        ticket = _sl_start(username, data["password"])
        self.set_cookie(_SL_COOKIE, ticket, path=self.hub.base_url, httponly=True,
                        secure=self.request.protocol == "https", samesite="Lax")
        nxt = self.get_argument("next", "")
        self.redirect(f"{self.hub.base_url}login/code" + (f"?next={url_escape(nxt)}" if nxt else ""))


class SignInCodeHandler(BaseHandler):
    """GET/POST /hub/login/code: the second step of signing in with 2FA."""

    async def _page(self, username=None, error="", ended=""):
        self.finish(await self.render_template(
            "login-code.html", username=username, error=error, ended=ended,
            next=url_escape(self.get_argument("next", ""))))

    def _end(self, ticket):
        _sl_drop(ticket)
        self.clear_cookie(_SL_COOKIE, path=self.hub.base_url)

    async def get(self):
        if self.current_user:
            return self.redirect(self.get_next_url(self.current_user))
        ticket = self.get_cookie(_SL_COOKIE)
        w = _sl_find(ticket)
        if w is None:
            self._end(ticket)
            return await self._page(ended=_SL_TIMED_OUT)
        await self._page(w["username"])

    async def post(self):
        ticket = self.get_cookie(_SL_COOKIE)
        w = _sl_find(ticket)
        if w is None:
            self._end(ticket)
            return await self._page(ended=_SL_TIMED_OUT)
        if self.get_body_argument("action", "") == "cancel":
            self._end(ticket)
            nxt = self.get_argument("next", "")
            return self.redirect(f"{self.hub.base_url}login" + (f"?next={url_escape(nxt)}" if nxt else ""))
        code = self.get_body_argument("code", "").replace(" ", "").strip()
        if not code:
            return await self._page(w["username"], error="Enter the 6-digit code from your authenticator app.")
        # Every check again, password and approval included, through the
        # authenticator as wrapped by the other modules (zai_caimex).
        user = await self.login_user(
            {"username": w["username"], "password": w["password"], "2fa": code})
        if user:
            self._end(ticket)
            self.log.info("2fa: %s signed in with a code", user.name)
            return self.redirect(self.get_next_url(user))
        w["tries"] += 1
        auth = self.authenticator
        if auth.allowed_failed_logins and auth.is_blocked(w["username"]):
            self._end(ticket)
            return await self._page(ended=_SL_LOCKED)
        if w["tries"] >= _SL_TRIES:
            self._end(ticket)
            return await self._page(ended=_SL_TOO_MANY)
        self.set_status(403)
        await self._page(w["username"], error=(
            "That code didn't match. Check your phone's clock is set automatically, "
            "then enter the code shown now."))


_na_get_handlers = None          # the original, captured by configure()


def _get_handlers_sl(self, app):
    handlers = [(route, TwoStepLoginHandler if route == r"/login" else handler, *rest)
                for route, handler, *rest in _na_get_handlers(self, app)]
    return handlers + [(r"/login/code", SignInCodeHandler)]


def configure(c):
    global _na_get_handlers
    # NativeAuthenticator.authenticate() calls this for users with has_2fa.
    UserInfo.is_valid_token = lambda self, token: verify(self, token)
    handlers = list(c.JupyterHub.get("extra_handlers", []) or [])
    handlers.append((r"/account/2fa", TwoFactorHandler))
    c.JupyterHub.extra_handlers = handlers
    # The authenticator's pages come before every other page, so /login is
    # replaced there. Wrapped once, however many times configure() runs.
    if _na_get_handlers is None:
        _na_get_handlers = NativeAuthenticator.get_handlers
        NativeAuthenticator.get_handlers = _get_handlers_sl
