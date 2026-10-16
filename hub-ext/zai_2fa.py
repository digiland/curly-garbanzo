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

API tokens are unaffected: 2FA guards the browser sign-in, not tokens a user
already made.

Loaded by extraConfig `03-zai-platform` next to zai_platform.py.
"""

import base64
import hashlib
import hmac
import io
import os
import struct
import time
from urllib.parse import quote

from jupyterhub.handlers.base import BaseHandler
from nativeauthenticator.orm import UserInfo
from tornado import web

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


def configure(c):
    # NativeAuthenticator.authenticate() calls this for users with has_2fa.
    UserInfo.is_valid_token = lambda self, token: verify(self, token)
    handlers = list(c.JupyterHub.get("extra_handlers", []) or [])
    handlers.append((r"/account/2fa", TwoFactorHandler))
    c.JupyterHub.extra_handlers = handlers
