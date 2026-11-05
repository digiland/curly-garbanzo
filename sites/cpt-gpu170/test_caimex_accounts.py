"""Tests for the Caimex account config in values.yaml (06-caimex-accounts) and
the template-variable fix in 05-two-factor.

The code under test is the text of those config blocks, executed here exactly
as the hub executes it, against a real NativeAuthenticator and a stand-in
Caimex that answers with the status codes and error shapes of the real one.

Unlike meter/test_meter.py this is not stdlib only. It needs the hub's packages:

    pip install jupyterhub jupyterhub-nativeauthenticator==1.2.0 pyyaml
    cd sites/cpt-gpu170 && python3 -m unittest -v test_caimex_accounts
"""
import asyncio
import html
import json
import os
import re
import types
import unittest
import urllib.parse

import onetimepass
import yaml
from jinja2 import DictLoader, Environment
from jupyterhub import orm
from nativeauthenticator import NativeAuthenticator
from nativeauthenticator.orm import UserInfo
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tornado import web
from tornado.httpserver import HTTPServer
from tornado.netutil import bind_sockets
from traitlets.config import Config

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
VALUES = yaml.safe_load(open(os.path.join(HERE, "values.yaml")))
BLOCKS = VALUES["hub"]["extraConfig"]
SETTINGS = ("CAIMEX_API_URL", "CAIMEX_WEB_URL", "CAIMEX_ORG_ID",
            "CAIMEX_SERVICE_EMAIL", "CAIMEX_SERVICE_PASSWORD")
SERVICE = {"email": "svc@zai.example", "password": "service-password-1"}

# The config blocks patch these in place. Restore them before each test so one
# test's patches never stack on another's.
_AUTHENTICATE = NativeAuthenticator.authenticate
_GET_HANDLERS = NativeAuthenticator.get_handlers


def template(name):
    return open(os.path.join(REPO, "templates", name)).read()


class FakeCaimex:
    """The four Caimex calls the hub makes, with the real API's behaviour."""

    def __init__(self):
        self.calls = []          # (what, detail), in order, accepted calls only
        self.accounts = {}       # email -> {"password", "member", "mfa", "name"}
        self.service_login_ok = True
        self.service_token = "service-token-1"
        self.register_status = None      # force a failure: 429 or 422
        fake = self

        class Login(web.RequestHandler):
            def post(self):
                b = json.loads(self.request.body)
                if b["email"] == SERVICE["email"]:
                    fake.calls.append(("service-login", None))
                    if fake.service_login_ok and b["password"] == SERVICE["password"]:
                        return self.finish({"access_token": fake.service_token, "user": {}})
                    self.set_status(401)
                    return self.finish({"detail": "Invalid email or password"})
                fake.calls.append(("user-login", b["email"]))
                account = fake.accounts.get(b["email"])
                if not account or account["password"] != b["password"]:
                    self.set_status(401)
                    return self.finish({"detail": "Invalid email or password"})
                if account.get("mfa"):
                    return self.finish({"access_token": None, "mfa_required": True,
                                        "mfa_token": "mfa-token"})
                self.finish({"access_token": "login-token:" + b["email"], "user": {}})

        class Invite(web.RequestHandler):
            def post(self, org):
                if self.request.headers.get("Authorization") != "Bearer " + fake.service_token:
                    self.set_status(401)
                    return self.finish({"detail": "Invalid or expired token"})
                b = json.loads(self.request.body)
                fake.calls.append(("invite", (org, b["email"], b["role"])))
                if fake.accounts.get(b["email"], {}).get("member"):
                    self.set_status(409)
                    return self.finish({"detail": "That person is already a member"})
                self.set_status(201)
                self.finish({"invite_token": "invite:" + b["email"], "email_sent": True})

        class Register(web.RequestHandler):
            def post(self):
                b = json.loads(self.request.body)
                fake.calls.append(("register", (b["email"], b["password"], b.get("invite_token"))))
                if fake.register_status == 422:
                    # What the live API returns: the rejected value under "input".
                    self.set_status(422)
                    return self.finish({"detail": [{
                        "type": "string_too_long", "loc": ["body", "password"],
                        "msg": "String should have at most 128 characters",
                        "input": b["password"]}]})
                if fake.register_status:
                    self.set_status(fake.register_status)
                    return self.finish({"detail": "Too many attempts"})
                if b["email"] in fake.accounts:
                    self.set_status(409)
                    return self.finish({"detail": "Email already registered"})
                fake.accounts[b["email"]] = {
                    "password": b["password"], "name": b.get("full_name"),
                    "member": b.get("invite_token") == "invite:" + b["email"]}
                self.set_status(201)
                self.finish({"access_token": "register-token:" + b["email"], "user": {}})

        self.app = web.Application([
            (r"/api/auth/login", Login), (r"/api/auth/register", Register),
            (r"/api/organizations/([^/]+)/invitations", Invite)])


class FakeHandler:
    """Just enough of a JupyterHub handler to call OpenCaimex.get on."""

    def __init__(self, name, db, args=None):
        self.current_user = types.SimpleNamespace(name=name)
        self.db = db
        self.hub = types.SimpleNamespace(base_url="/hub/")
        self.args = args or {}
        self.headers, self.redirected_to, self.body = {}, None, None
        self.rendered, self.cookie_cleared = None, False

    def get_argument(self, name, default=None):
        return self.args.get(name, default)

    def set_header(self, key, value):
        self.headers[key] = value

    def redirect(self, url):
        self.redirected_to = url

    def finish(self, body=None):
        self.body = body

    async def render_template(self, name, **kwargs):
        self.rendered = (name, kwargs)
        return "rendered:" + name

    def clear_login_cookie(self):
        self.cookie_cleared = True

    @property
    def forward_url(self):
        """Where the hand-off page sends the browser, or None."""
        m = re.search(r'http-equiv="refresh" content="0;url=([^"]+)"', self.body or "")
        return html.unescape(m.group(1)) if m else None

    @property
    def token(self):
        """The Caimex session the hand-off page carries, or None."""
        if not self.forward_url:
            return None
        return urllib.parse.parse_qs(self.forward_url.split("#", 1)[1])["token"][0]


class CaimexCase(unittest.IsolatedAsyncioTestCase):
    settings = None      # override in a subclass to test other configurations

    async def asyncSetUp(self):
        self.caimex = FakeCaimex()
        sockets = bind_sockets(0, "127.0.0.1")
        self.server = HTTPServer(self.caimex.app)
        self.server.add_sockets(sockets)
        port = sockets[0].getsockname()[1]
        env = {"CAIMEX_API_URL": f"http://127.0.0.1:{port}/", "CAIMEX_WEB_URL": "https://caimex.test/",
               "CAIMEX_ORG_ID": "org-1", "CAIMEX_SERVICE_EMAIL": SERVICE["email"],
               "CAIMEX_SERVICE_PASSWORD": SERVICE["password"]}
        env.update(self.settings or {})
        self._old_env = {k: os.environ.get(k) for k in SETTINGS}
        os.environ.update(env)

        NativeAuthenticator.authenticate = _AUTHENTICATE
        NativeAuthenticator.get_handlers = _GET_HANDLERS
        engine = create_engine("sqlite://")
        orm.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.config = Config()
        self.config.JupyterHub.template_vars = {}
        self.ns = {"c": self.config}
        exec(BLOCKS["06-caimex-accounts"], self.ns)

        self.auth = NativeAuthenticator(db=self.db, ask_email_on_signup=True,
                                        open_signup=False, minimum_password_length=12)
        self.handlers = NativeAuthenticator.get_handlers(
            self.auth, types.SimpleNamespace(db=self.db))
        # As JupyterHub builds a page: callables are called with the user.
        self.page_vars = {k: (v(None) if callable(v) else v)
                          for k, v in self.config.JupyterHub.template_vars.items()}

    async def asyncTearDown(self):
        self.server.stop()
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        NativeAuthenticator.authenticate = _AUTHENTICATE
        NativeAuthenticator.get_handlers = _GET_HANDLERS

    # -- helpers ----------------------------------------------------------
    def add_user(self, name, password, email=None, approved=True, two_factor=False):
        self.assertIsNotNone(self.auth.create_user(
            name, password, email=email or f"{name}@zai.example", has_2fa=two_factor))
        info = UserInfo.find(self.db, name)
        info.is_authorized = approved
        self.db.commit()
        return info

    async def sign_in(self, name, password, code=None):
        data = {"username": name, "password": password}
        if code is not None:
            data["2fa"] = code
        result = await self.auth.authenticate(None, data)
        await self.settle()
        return result

    async def settle(self):
        for _ in range(200):
            if not self.ns["_cx_running"]:
                return
            await asyncio.sleep(0.02)
        self.fail("Caimex work did not finish")

    def status(self, name):
        return self.page_vars["caimex_status"](name)

    async def open_caimex(self, name, **args):
        handler = FakeHandler(name, self.db, args)
        await self.ns["OpenCaimex"].get(handler)
        return handler

    def what(self):
        return [call[0] for call in self.caimex.calls]


class FirstSignIn(CaimexCase):
    async def test_creates_the_account_inside_the_organization(self):
        self.add_user("alice", "alice-password-1", email="Alice@Zai.Example")
        self.assertEqual(await self.sign_in("alice", "alice-password-1"), "alice")
        self.assertEqual(self.caimex.calls, [
            ("service-login", None),
            ("invite", ("org-1", "alice@zai.example", "member")),
            ("register", ("alice@zai.example", "alice-password-1", "invite:alice@zai.example"))])
        self.assertEqual(self.caimex.accounts["alice@zai.example"],
                         {"password": "alice-password-1", "name": "alice", "member": True})
        self.assertEqual(self.status("alice"), ("created", ""))

    async def test_wrong_password_never_reaches_caimex(self):
        self.add_user("alice", "alice-password-1")
        self.assertIsNone(await self.sign_in("alice", "not-her-password"))
        self.assertEqual(self.caimex.calls, [])

    async def test_unapproved_user_never_reaches_caimex(self):
        self.add_user("dave", "dave-password-12", approved=False)
        self.assertIsNone(await self.sign_in("dave", "dave-password-12"))
        self.assertEqual(self.caimex.calls, [])
        self.assertEqual(self.status("dave"), (None, ""))

    async def test_hub_two_factor_does_not_get_in_the_way(self):
        info = self.add_user("tina", "tina-password-12", two_factor=True)
        self.assertIsNone(await self.sign_in("tina", "tina-password-12", code="000000"))
        self.assertEqual(self.caimex.calls, [])
        code = str(onetimepass.get_totp(info.otp_secret)).zfill(6)
        self.assertEqual(await self.sign_in("tina", "tina-password-12", code=code), "tina")
        self.assertEqual(self.status("tina"), ("created", ""))
        self.assertEqual((await self.open_caimex("tina")).token, "register-token:tina@zai.example")

    async def test_user_without_an_email_is_recorded_not_sent(self):
        self.add_user("noemail", "noemail-password")
        UserInfo.find(self.db, "noemail").email = ""
        self.db.commit()
        await self.sign_in("noemail", "noemail-password")
        self.assertEqual(self.caimex.calls, [])
        self.assertEqual(self.status("noemail"), ("failed", "No email address on file."))


class ExistingCaimexAccounts(CaimexCase):
    async def test_different_password_is_left_alone(self):
        self.add_user("bob", "bob-password-123")
        self.caimex.accounts["bob@zai.example"] = {"password": "his-own-caimex-pw", "member": False}
        await self.sign_in("bob", "bob-password-123")
        self.assertEqual(self.what(), ["service-login", "invite", "register", "user-login"])
        self.assertEqual(self.caimex.accounts["bob@zai.example"]["password"], "his-own-caimex-pw")
        self.assertEqual(self.status("bob"), ("invited", ""))
        # One refused attempt, then the hub stops knocking on his account.
        self.caimex.calls.clear()
        await self.sign_in("bob", "bob-password-123")
        await self.sign_in("bob", "bob-password-123")
        self.assertEqual(self.caimex.calls, [])
        self.assertEqual((await self.open_caimex("bob")).redirected_to,
                         "https://caimex.test/org/org-1/overview")

    async def test_member_with_the_same_password_gets_one_click(self):
        self.add_user("carol", "carol-password-1")
        self.caimex.accounts["carol@zai.example"] = {"password": "carol-password-1", "member": True}
        await self.sign_in("carol", "carol-password-1")
        self.assertEqual(self.what(), ["service-login", "invite", "user-login"])
        self.assertEqual(self.status("carol"), ("invited", ""))
        self.assertEqual((await self.open_caimex("carol")).token, "login-token:carol@zai.example")

    async def test_two_factor_on_the_caimex_account(self):
        self.add_user("gina", "gina-password-12")
        await self.sign_in("gina", "gina-password-12")
        self.ns["_cx_sessions"].clear()
        self.caimex.accounts["gina@zai.example"]["mfa"] = True
        self.caimex.calls.clear()
        await self.sign_in("gina", "gina-password-12")
        self.assertEqual(self.caimex.calls, [("user-login", "gina@zai.example")])
        self.assertIsNone((await self.open_caimex("gina")).forward_url)
        self.caimex.calls.clear()
        await self.sign_in("gina", "gina-password-12")
        self.assertEqual(self.caimex.calls, [])
        self.assertEqual(self.status("gina"), ("created", ""))


class OpenCaimex(CaimexCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.add_user("alice", "alice-password-1")
        await self.sign_in("alice", "alice-password-1")

    async def test_forwards_with_the_users_own_token(self):
        handler = await self.open_caimex("alice")
        base, fragment = handler.forward_url.split("#", 1)
        self.assertEqual(base, "https://caimex.test/auth/callback")
        self.assertEqual(urllib.parse.parse_qs(fragment), {
            "token": ["register-token:alice@zai.example"], "redirect": ["/org/org-1/overview"]})
        self.assertEqual(handler.headers["Cache-Control"], "no-store")
        self.assertEqual(handler.headers["Referrer-Policy"], "no-referrer")

    async def test_token_is_never_put_in_a_redirect(self):
        # JupyterHub writes every redirect target to its access log.
        self.assertIsNone((await self.open_caimex("alice")).redirected_to)

    async def test_another_user_gets_nothing_of_alices(self):
        self.add_user("bob", "bob-password-123")
        handler = await self.open_caimex("bob")
        self.assertIsNone(handler.forward_url)
        self.assertNotIn("alice", str(handler.redirected_to) + str(handler.body))

    async def test_repeat_sign_in_reuses_the_session(self):
        self.caimex.calls.clear()
        await self.sign_in("alice", "alice-password-1")
        self.assertEqual(self.caimex.calls, [])

    async def test_without_a_session_it_offers_to_sign_in_again(self):
        self.ns["_cx_sessions"].clear()        # a hub restart, or a day-old tab
        handler = await self.open_caimex("alice")
        self.assertIsNone(handler.forward_url)
        name, variables = handler.rendered
        self.assertEqual(name, "caimex.html")
        self.assertEqual(variables, {"again_url": "/hub/caimex/again",
                                     "caimex_url": "https://caimex.test/org/org-1/overview"})

    async def test_it_only_offers_once(self):
        self.ns["_cx_sessions"].clear()
        handler = await self.open_caimex("alice", tried="1")
        self.assertIsNone(handler.rendered)
        self.assertEqual(handler.redirected_to, "https://caimex.test/org/org-1/overview")

    async def test_sign_in_again_returns_to_open_caimex(self):
        handler = FakeHandler("alice", self.db)
        await self.ns["OpenCaimexAgain"].get(handler)
        self.assertTrue(handler.cookie_cleared)
        self.assertEqual(handler.redirected_to, "/hub/login?next=%2Fhub%2Fcaimex%3Ftried%3D1")

    async def test_next_sign_in_restores_one_click(self):
        self.ns["_cx_sessions"].clear()
        self.caimex.calls.clear()
        await self.sign_in("alice", "alice-password-1")
        self.assertEqual(self.caimex.calls, [("user-login", "alice@zai.example")])
        self.assertEqual((await self.open_caimex("alice")).token, "login-token:alice@zai.example")

    async def test_a_click_right_after_sign_in_waits_for_the_account(self):
        self.add_user("erin", "erin-password-12")
        # No settle(): the Caimex work is still running when the link is clicked.
        await self.auth.authenticate(None, {"username": "erin", "password": "erin-password-12"})
        self.assertIn("erin", self.ns["_cx_running"])
        handler = await self.open_caimex("erin")
        self.assertEqual(handler.token, "register-token:erin@zai.example")


class Failures(CaimexCase):
    async def test_service_account_refused_then_recovers(self):
        self.add_user("erin", "erin-password-12")
        self.caimex.service_login_ok = False
        self.assertEqual(await self.sign_in("erin", "erin-password-12"), "erin")
        status, detail = self.status("erin")
        self.assertEqual(status, "failed")
        self.assertIn("service sign-in refused", detail)
        self.assertNotIn("erin@zai.example", self.caimex.accounts)
        # Refusals are not retried for five minutes; skip the wait.
        self.caimex.service_login_ok = True
        self.ns["_cx"]["login_failed"] = None
        await self.sign_in("erin", "erin-password-12")
        self.assertEqual(self.status("erin"), ("created", ""))

    async def test_service_account_refusal_is_not_hammered(self):
        self.add_user("erin", "erin-password-12")
        self.caimex.service_login_ok = False
        await self.sign_in("erin", "erin-password-12")
        await self.sign_in("erin", "erin-password-12")
        self.assertEqual(self.what(), ["service-login"])

    async def test_service_account_with_two_factor_says_so(self):
        self.caimex.accounts[SERVICE["email"]] = {}

        async def mfa(path, body, token=None):
            return 200, {"access_token": None, "mfa_required": True}
        self.ns["_cx_fetch"] = mfa
        self.add_user("erin", "erin-password-12")
        await self.sign_in("erin", "erin-password-12")
        self.assertIn("two-factor", self.status("erin")[1])

    async def test_expired_service_token_signs_in_again_once(self):
        self.add_user("alice", "alice-password-1")
        await self.sign_in("alice", "alice-password-1")
        self.caimex.service_token = "service-token-2"      # the old one is now refused
        self.caimex.calls.clear()
        self.add_user("frank", "frank-password-1")
        await self.sign_in("frank", "frank-password-1")
        self.assertEqual(self.what(), ["service-login", "invite", "register"])
        self.assertEqual(self.status("frank"), ("created", ""))

    async def test_register_throttled_then_retried_at_next_sign_in(self):
        self.add_user("gina", "gina-password-12")
        self.caimex.register_status = 429
        await self.sign_in("gina", "gina-password-12")
        self.assertEqual(self.status("gina"), ("failed", "register: HTTP 429 Too many attempts"))
        self.caimex.register_status = None
        await self.sign_in("gina", "gina-password-12")
        self.assertEqual(self.status("gina"), ("created", ""))

    async def test_caimex_unreachable_never_blocks_the_hub(self):
        self.add_user("hank", "hank-password-12")
        self.ns["_CX_URL"] = "http://127.0.0.1:1"
        self.assertEqual(await self.sign_in("hank", "hank-password-12"), "hank")
        self.assertEqual(self.status("hank"), ("failed", "invitation: Caimex could not be reached"))

    async def test_an_unexpected_error_is_recorded_without_its_text(self):
        self.add_user("ivan", "ivan-password-12")

        async def boom(*args):
            raise RuntimeError("ivan-password-12 leaked into an error message")
        self.ns["_cx_register"] = boom
        self.assertEqual(await self.sign_in("ivan", "ivan-password-12"), "ivan")
        self.assertEqual(self.status("ivan"), ("failed", "Internal error: RuntimeError"))


class Secrets(CaimexCase):
    def table_text(self):
        rows = self.db.execute(self.ns["_cx_table"].select()).fetchall()
        return json.dumps([[str(value) for value in row] for row in rows])

    async def test_a_password_caimex_rejects_is_never_recorded(self):
        # The live API answers a rejected register with the value it rejected.
        self.add_user("lena", "lena-password-12")
        self.caimex.register_status = 422
        with self.assertLogs(level="ERROR") as logs:
            await self.sign_in("lena", "lena-password-12")
        self.assertEqual(self.status("lena"), (
            "failed", "register: HTTP 422 password: String should have at most 128 characters"))
        self.assertNotIn("lena-password-12", self.table_text())
        self.assertNotIn("lena-password-12", "\n".join(logs.output))

    def test_reason_never_copies_the_rejected_input(self):
        reason = self.ns["_cx_reason"](422, {"detail": [
            {"loc": ["body", "password"], "msg": "too long", "input": "the-secret"},
            {"loc": ["body", "email"], "msg": "not an email", "input": "also-secret"}]})
        self.assertEqual(reason, "HTTP 422 password: too long; email: not an email")
        self.assertEqual(self.ns["_cx_reason"](400, {"detail": {"input": "the-secret"}}), "HTTP 400")
        self.assertEqual(self.ns["_cx_reason"](401, {"access_token": "t", "detail": "No"}), "HTTP 401 No")

    async def test_status_table_holds_no_passwords_or_tokens(self):
        self.add_user("alice", "alice-password-1")
        self.add_user("bob", "bob-password-123")
        self.caimex.accounts["bob@zai.example"] = {"password": "his-own-caimex-pw", "member": False}
        await self.sign_in("alice", "alice-password-1")
        await self.sign_in("bob", "bob-password-123")
        text = self.table_text()
        for secret in ("alice-password-1", "bob-password-123", SERVICE["password"],
                       "register-token", "login-token", "service-token", "invite:"):
            self.assertNotIn(secret, text)


class IncompleteSettings(CaimexCase):
    settings = {"CAIMEX_WEB_URL": ""}

    async def test_feature_is_off(self):
        self.assertFalse(self.ns["_CX_ON"])
        self.assertFalse(self.page_vars["caimex_on"])
        self.add_user("alice", "alice-password-1")
        self.assertEqual(await self.sign_in("alice", "alice-password-1"), "alice")
        self.assertEqual(self.caimex.calls, [])
        with self.assertRaises(web.HTTPError) as raised:
            await self.open_caimex("alice")
        self.assertEqual(raised.exception.status_code, 404)


class PlainHttpAddress(CaimexCase):
    settings = {"CAIMEX_API_URL": "http://caimex.example.com"}

    async def test_passwords_are_never_sent_unencrypted(self):
        self.assertFalse(self.ns["_CX_ON"])
        self.add_user("alice", "alice-password-1")
        await self.sign_in("alice", "alice-password-1")
        self.assertEqual(self.caimex.calls, [])


class Pages(CaimexCase):
    def render(self, name, **variables):
        env = Environment(autoescape=True, loader=DictLoader({
            "page.html": "{% block main %}{% endblock %}", name: template(name)}))
        return env.get_template(name).render(base_url="/hub/", **variables)

    async def test_approve_page_shows_each_state(self):
        for name in ("created", "invited", "failed", "waiting"):
            self.add_user(name, name + "-password-12")
        for name in ("created", "invited"):
            self.ns["_cx_set"](self.db, name, name + "@zai.example", name)
        self.ns["_cx_set"](self.db, "failed", "failed@zai.example", "failed", '"><script>x</script>')
        page = self.render("authorization-area.html", ask_email=True,
                           users=self.db.query(UserInfo).all(), **self.page_vars)
        for text in ("Caimex account", ">created<", ">invited<",
                     "failed, retried at next sign-in", "created at first sign-in"):
            self.assertIn(text, page)
        self.assertNotIn("<script>x</script>", page)

    async def test_approve_page_without_the_feature(self):
        self.add_user("alice", "alice-password-1")
        page = self.render("authorization-area.html", ask_email=True,
                           users=self.db.query(UserInfo).all())
        self.assertNotIn("Caimex", page)

    def test_sign_in_again_page(self):
        page = self.render("caimex.html", again_url="/hub/caimex/again",
                           caimex_url="https://caimex.test/org/org-1/overview")
        self.assertIn('href="/hub/caimex/again"', page)
        self.assertIn('href="https://caimex.test/org/org-1/overview"', page)

    def test_nav_link_and_signup_note_are_conditional(self):
        self.assertIn('href="{{base_url}}caimex"', template("page.html"))
        self.assertIn("caimex_on is defined and caimex_on", template("page.html"))
        self.assertIn("creates your Caimex account", template("signup.html"))
        # Existing users never see the signup page; the login page tells them.
        login = template("native-login.html")
        self.assertIn("sets up your Caimex account", login)
        self.assertIn("caimex_on is defined and caimex_on", login)

    def test_handlers_are_registered_beside_the_authenticators(self):
        paths = [handler[0] for handler in self.handlers]
        for path in (r"/caimex", r"/caimex/again", r"/authorize/([^/]*)", r"/login"):
            self.assertIn(path, paths)


class TemplateVariables(unittest.TestCase):
    """JupyterHub calls every callable in template_vars with the current user
    and gives the template the result. A function the template is meant to call
    must therefore be returned from one. Getting this wrong is a 500."""

    def page_vars(self, block):
        config = Config()
        config.JupyterHub.template_vars = {}
        exec(BLOCKS[block], {"c": config})
        return {k: (v(None) if callable(v) else v)
                for k, v in config.JupyterHub.template_vars.items()}

    def tearDown(self):
        NativeAuthenticator.authenticate = _AUTHENTICATE
        NativeAuthenticator.get_handlers = _GET_HANDLERS

    def test_two_factor_qr_helper_is_callable_from_the_signup_page(self):
        totp_qr = self.page_vars("05-two-factor")["totp_qr"]
        self.assertTrue(callable(totp_qr))
        totp_qr("otpauth://totp/ZAI:alice?secret=ABC&issuer=ZAI")     # must not raise

    def test_caimex_status_is_callable_from_the_approve_page(self):
        self.assertTrue(callable(self.page_vars("06-caimex-accounts")["caimex_status"]))


class Wiring(unittest.TestCase):
    def test_every_setting_is_an_optional_secret(self):
        for name in SETTINGS:
            ref = VALUES["hub"]["extraEnv"][name]["valueFrom"]["secretKeyRef"]
            self.assertEqual(ref["name"], "caimex-provision")
            self.assertTrue(ref["optional"], name)

    def test_deploy_script_writes_the_keys_the_hub_reads(self):
        deploy = open(os.path.join(HERE, "deploy.sh")).read()
        for name in SETTINGS:
            key = VALUES["hub"]["extraEnv"][name]["valueFrom"]["secretKeyRef"]["key"]
            self.assertIn(f"--from-literal={key}=", deploy)
            self.assertIn("${%s" % name, deploy.replace('"$%s"' % name, "${%s}" % name))

    def test_env_example_lists_every_setting(self):
        example = open(os.path.join(REPO, ".env.example")).read()
        for name in SETTINGS:
            self.assertIn(name + "=", example)


if __name__ == "__main__":
    unittest.main()
