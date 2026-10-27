"""Tests for signing in in two steps (hub-ext/zai_2fa.py).

They run against a real NativeAuthenticator in a throwaway database and
zai_2fa's real code check; the pages' handlers are driven through a stand-in
for the request. It needs the hub's packages:

    pip install jupyterhub jupyterhub-nativeauthenticator==1.2.0 pyyaml
    cd hub-ext && python3 -m unittest -v test_zai_2fa
"""
import asyncio
import base64
import os
import time
import types
import unittest

from jinja2 import DictLoader, Environment
from jupyterhub import orm
from nativeauthenticator import NativeAuthenticator
from nativeauthenticator.handlers import LoginHandler
from nativeauthenticator.orm import UserInfo
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from traitlets.config import Config

import zai_2fa  # noqa: E402  (hub-ext is this file's own directory)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PASSWORD = "old-password-123"


def code_for(info, at=None):
    secret = info.otp_secret.upper()
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    return zai_2fa._code(key, int((at or time.time()) // 30))


class Case(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://")
        orm.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        UserInfo.__table__.create(engine, checkfirst=True)
        self.auth = NativeAuthenticator(db=self.db, allowed_failed_logins=5, seconds_before_next_try=900)
        zai_2fa.configure(Config())
        zai_2fa._last_used.clear()
        zai_2fa._sl_waiting.clear()

    def user(self, name="tendai", two_factor=True, approved=True):
        info = self.auth.create_user(name, PASSWORD)
        info.is_authorized = approved
        if two_factor:
            info.has_2fa, info.otp_secret = True, zai_2fa.new_secret()
        self.db.commit()
        return info

    def first_step(self, name="tendai", password=PASSWORD, **more):
        return zai_2fa._sl_needs_code(self.auth, dict(username=name, password=password, **more))


class FirstStep(Case):
    def test_right_password_with_two_factor_goes_to_the_code_page(self):
        self.user()
        self.assertEqual(self.first_step(), "tendai")
        self.assertEqual(self.first_step(name="Tendai"), "tendai")

    def test_everything_else_is_left_to_the_sign_in_page(self):
        self.user()
        self.user("rudo", two_factor=False)
        self.user("waiting", approved=False)
        self.assertIsNone(self.first_step(password="wrong-password"))
        self.assertIsNone(self.first_step(name="rudo"))          # no 2FA: signs in as before
        self.assertIsNone(self.first_step(name="waiting"))       # told it awaits approval, as before
        self.assertIsNone(self.first_step(name="nobody"))
        self.assertIsNone(self.first_step(name=""))
        self.assertIsNone(self.first_step(**{"2fa": "123456"}))  # a code sent with the password: one step

    def test_a_locked_account_is_not_sent_on(self):
        self.user()
        for _ in range(5):
            self.auth.add_login_attempt("tendai")
        self.assertIsNone(self.first_step())


class Waiting(Case):
    def test_only_a_hash_is_kept_and_a_new_sign_in_replaces_the_old(self):
        first = zai_2fa._sl_start("tendai", PASSWORD)
        self.assertNotIn(first, zai_2fa._sl_waiting)
        second = zai_2fa._sl_start("tendai", PASSWORD)
        self.assertIsNone(zai_2fa._sl_find(first))
        self.assertEqual(zai_2fa._sl_find(second)["username"], "tendai")
        self.assertEqual(len(zai_2fa._sl_waiting), 1)

    def test_it_lasts_five_minutes(self):
        now = time.time()
        ticket = zai_2fa._sl_start("tendai", PASSWORD, now=now)
        self.assertIsNotNone(zai_2fa._sl_find(ticket, now=now + 299))
        self.assertIsNone(zai_2fa._sl_find(ticket, now=now + 301))
        self.assertEqual(zai_2fa._sl_waiting, {})                 # and the password with it

    def test_unknown_or_missing_cookie(self):
        self.assertIsNone(zai_2fa._sl_find("made-up"))
        self.assertIsNone(zai_2fa._sl_find(None))


class FakeCodePage:
    """Enough of a request for SignInCodeHandler: what it was sent, and what
    it answered."""
    get = zai_2fa.SignInCodeHandler.get
    post = zai_2fa.SignInCodeHandler.post
    _page = zai_2fa.SignInCodeHandler._page
    _end = zai_2fa.SignInCodeHandler._end

    def __init__(self, auth, cookie=None, **body):
        self.authenticator, self.cookie, self.body = auth, cookie, body
        self.hub = types.SimpleNamespace(base_url="/hub/")
        self.log = types.SimpleNamespace(info=lambda *a: None)
        self.current_user = None
        self.status, self.redirected, self.page, self.signed_in_with = 200, None, None, None

    def get_cookie(self, name):
        return self.cookie

    def clear_cookie(self, name, path=None):
        self.cookie = None

    def get_body_argument(self, name, default=""):
        return self.body.get(name, default)

    def get_argument(self, name, default=""):
        return self.body.get(name, default)

    async def login_user(self, data):
        self.signed_in_with = data
        name = await self.authenticator.authenticate(self, data)
        return types.SimpleNamespace(name=name) if name else None

    def get_next_url(self, user):
        return "/hub/home"

    def redirect(self, url):
        self.redirected = url

    def set_status(self, status):
        self.status = status

    async def render_template(self, name, **variables):
        return variables

    def finish(self, page):
        self.page = page


class CodePage(Case):
    def setUp(self):
        super().setUp()
        self.info = self.user()
        self.ticket = zai_2fa._sl_start("tendai", PASSWORD)

    def send(self, cookie="ticket", **body):
        page = FakeCodePage(self.auth, self.ticket if cookie == "ticket" else cookie, **body)
        asyncio.run(page.post())
        return page

    def test_shows_who_is_signing_in(self):
        page = FakeCodePage(self.auth, self.ticket)
        asyncio.run(page.get())
        self.assertEqual(page.page["username"], "tendai")
        self.assertFalse(page.page["ended"])

    def test_the_right_code_signs_in_with_every_check(self):
        page = self.send(code=code_for(self.info))
        self.assertEqual(page.redirected, "/hub/home")
        # The password goes through the authenticator again, so zai_caimex gets it.
        self.assertEqual(page.signed_in_with["password"], PASSWORD)
        self.assertIsNone(page.cookie)
        self.assertEqual(zai_2fa._sl_waiting, {})

    def test_an_empty_code_says_so(self):
        page = self.send(code="  ")
        self.assertIn("Enter the 6-digit code", page.page["error"])
        self.assertIsNone(page.signed_in_with)                    # not a failed attempt
        self.assertIsNotNone(zai_2fa._sl_find(self.ticket))

    def test_a_wrong_code_says_so_and_counts_towards_the_lockout(self):
        page = self.send(code="000000")
        self.assertEqual(page.status, 403)
        self.assertIn("That code didn't match", page.page["error"])
        self.assertEqual(self.auth.login_attempts["tendai"]["count"], 1)
        self.assertIsNotNone(zai_2fa._sl_find(self.ticket))

    def test_five_wrong_codes_lock_the_account(self):
        for _ in range(4):
            self.send(code="000000")
        page = self.send(code="000000")
        self.assertEqual(page.page["ended"], zai_2fa._SL_LOCKED)
        self.assertEqual(zai_2fa._sl_waiting, {})
        self.assertTrue(self.auth.is_blocked("tendai"))
        # Locked: the right code does not help until the lockout ends.
        self.assertIsNone(asyncio.run(self.auth.authenticate(
            None, {"username": "tendai", "password": PASSWORD, "2fa": code_for(self.info)})))

    def test_five_wrong_codes_end_the_sign_in_even_without_a_lockout(self):
        self.auth.allowed_failed_logins = 0
        for _ in range(4):
            self.send(code="000000")
        self.assertEqual(self.send(code="000000").page["ended"], zai_2fa._SL_TOO_MANY)

    def test_timed_out_or_no_sign_in_waiting(self):
        zai_2fa._sl_waiting.clear()
        self.assertEqual(self.send(code=code_for(self.info)).page["ended"], zai_2fa._SL_TIMED_OUT)
        page = FakeCodePage(self.auth, None)
        asyncio.run(page.get())
        self.assertEqual(page.page["ended"], zai_2fa._SL_TIMED_OUT)

    def test_back_to_sign_in_forgets_it(self):
        page = self.send(action="cancel", next="/hub/spawn")
        self.assertEqual(page.redirected, "/hub/login?next=%2Fhub%2Fspawn")
        self.assertEqual(zai_2fa._sl_waiting, {})

    def test_a_password_changed_meanwhile_is_not_signed_in_with(self):
        self.auth.change_password("tendai", "a-different-passphrase")
        page = self.send(code=code_for(self.info))
        self.assertIsNone(page.redirected)
        self.assertIn("That code didn't match", page.page["error"])


class Pages(Case):
    def test_login_is_replaced_and_the_code_page_added(self):
        routes = dict(zai_2fa._get_handlers_sl(self.auth, None))
        self.assertIs(routes["/login"], zai_2fa.TwoStepLoginHandler)
        self.assertTrue(issubclass(zai_2fa.TwoStepLoginHandler, LoginHandler))
        self.assertIs(routes["/login/code"], zai_2fa.SignInCodeHandler)
        self.assertIn("/signup", routes)

    def render(self, name, **variables):
        env = Environment(autoescape=True, loader=DictLoader({
            "page.html": "{% block stylesheet %}{% endblock %}{% block main %}{% endblock %}{% block script %}{% endblock %}",
            "zai-panel.html": "",
            name: open(os.path.join(REPO, "templates", name)).read()}))
        return env.get_template(name).render(base_url="/hub/", xsrf="X", **variables)

    def test_sign_in_page_has_no_code_field(self):
        page = self.render("native-login.html", login_url="/hub/login", next="", username="")
        self.assertNotIn('name="2fa"', page)
        self.assertIn('name="password"', page)

    def test_code_page_states(self):
        form = self.render("login-code.html", username="tendai", error="", ended="", next="%2Fhub%2Fspawn")
        self.assertIn('action="/hub/login/code?next=%2Fhub%2Fspawn"', form)
        self.assertIn('name="code"', form)
        self.assertIn('value="cancel"', form)
        ended = self.render("login-code.html", username=None, error="", ended=zai_2fa._SL_TIMED_OUT, next="")
        self.assertIn("Sign-in timed out", ended)
        self.assertNotIn('name="code"', ended)


if __name__ == "__main__":
    unittest.main()
