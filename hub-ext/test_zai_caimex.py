"""Tests for hub-ext/zai_caimex.py: a Caimex account at first sign-in, and
one-click Open Caimex.

The module is loaded and configured here as the hub does it, against a real
NativeAuthenticator and a stand-in Caimex that answers with the status codes
and error shapes of the real one. It needs the hub's packages:

    pip install jupyterhub jupyterhub-nativeauthenticator==1.2.0 pyyaml
    cd hub-ext && python3 -m unittest -v test_zai_caimex
"""
import asyncio
import html
import importlib
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
REPO = os.path.dirname(HERE)
VALUES = yaml.safe_load(open(os.path.join(REPO, "charts", "jupyterhub-values.yaml")))

import zai_caimex  # noqa: E402  (hub-ext is this file's own directory)
# Not a secret, but restored around each test like the five that are.
EXTRA_SETTINGS = ("CAIMEX_FREE_ALLOWANCE_USD", "CAIMEX_ALLOWANCE_SWEEP_SECONDS")
SETTINGS = ("CAIMEX_API_URL", "CAIMEX_WEB_URL", "CAIMEX_ORG_ID",
            "CAIMEX_SERVICE_EMAIL", "CAIMEX_SERVICE_PASSWORD")
SERVICE = {"email": "svc@zai.example", "password": "service-password-1"}

# The module patches these in place. Restore them before each test so one
# test's patches never stack on another's.
_AUTHENTICATE = NativeAuthenticator.authenticate
_GET_HANDLERS = NativeAuthenticator.get_handlers


def template(name):
    return open(os.path.join(REPO, "templates", name)).read()


class FakeCaimex:
    """The Caimex calls the hub makes, with the real API's behaviour."""

    def __init__(self):
        self.calls = []          # (what, detail), in order, accepted calls only
        # The organisation's members, as GET .../members returns them. Money
        # comes back as strings, as Caimex's Decimal fields do.
        self.members = []
        self.can_set_caps = True         # False: the service account is not an Admin
        self.cap_failures = set()        # member ids whose cap update fails
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
                if fake.accounts[b["email"]]["member"]:
                    fake.add_member(b["email"])
                self.set_status(201)
                self.finish({"access_token": "register-token:" + b["email"], "user": {}})

        class Members(web.RequestHandler):
            def get(self, org):
                if self.request.headers.get("Authorization") != "Bearer " + fake.service_token:
                    self.set_status(401)
                    return self.finish({"detail": "Invalid or expired token"})
                fake.calls.append(("members", org))
                self.finish({"organization_id": org, "members": fake.members})

        class Entitlements(web.RequestHandler):
            def patch(self, org, member_id):
                if self.request.headers.get("Authorization") != "Bearer " + fake.service_token:
                    self.set_status(401)
                    return self.finish({"detail": "Invalid or expired token"})
                if not fake.can_set_caps:
                    self.set_status(403)
                    return self.finish({"detail": "Your organization role does not allow this (billing.manage)"})
                if member_id in fake.cap_failures:
                    self.set_status(500)
                    return self.finish({"detail": "internal error"})
                member = next((m for m in fake.members if m["id"] == member_id), None)
                if member is None:
                    self.set_status(404)
                    return self.finish({"detail": "Member not found"})
                b = json.loads(self.request.body)
                fake.calls.append(("cap", (member["email"], b)))
                if "spend_limit" in b:
                    member["spend_limit"] = b["spend_limit"]
                member["spend_period"] = b.get("spend_period", member["spend_period"])
                if b.get("reset_spend_used"):
                    member["spend_used"] = "0"
                self.finish(member)

        self.app = web.Application([
            (r"/api/auth/login", Login), (r"/api/auth/register", Register),
            (r"/api/organizations/([^/]+)/invitations", Invite),
            (r"/api/organizations/([^/]+)/members", Members),
            (r"/api/organizations/([^/]+)/members/([^/]+)/entitlements", Entitlements)])

    def add_member(self, email, role="member", spend_limit=None, spend_used="0", user_id=None):
        member = {"id": "member-%d" % (len(self.members) + 1), "user_id": user_id or "user:" + email,
                  "email": email, "role": role, "status": "active", "spend_limit": spend_limit,
                  "spend_used": spend_used, "spend_period": "lifetime"}
        self.members.append(member)
        return member


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
        self._old_env = {k: os.environ.get(k) for k in SETTINGS + EXTRA_SETTINGS}
        for key in EXTRA_SETTINGS:
            os.environ.pop(key, None)
        os.environ.update(env)

        NativeAuthenticator.authenticate = _AUTHENTICATE
        NativeAuthenticator.get_handlers = _GET_HANDLERS
        engine = create_engine("sqlite://")
        orm.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.config = Config()
        self.config.JupyterHub.template_vars = {}
        # A fresh copy of the module each time: no state carries between tests.
        module = importlib.reload(zai_caimex)
        module.configure(self.config)
        self.ns = vars(module)

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

    def tearDown(self):
        NativeAuthenticator.authenticate = _AUTHENTICATE
        NativeAuthenticator.get_handlers = _GET_HANDLERS

    def test_caimex_status_is_callable_from_the_approve_page(self):
        NativeAuthenticator.authenticate = _AUTHENTICATE
        NativeAuthenticator.get_handlers = _GET_HANDLERS
        config = Config()
        config.JupyterHub.template_vars = {"kept": 1}
        importlib.reload(zai_caimex).configure(config)
        page_vars = {k: (v(None) if callable(v) else v)
                     for k, v in config.JupyterHub.template_vars.items()}
        self.assertTrue(callable(page_vars["caimex_status"]))
        self.assertEqual(page_vars["kept"], 1)     # other modules' variables survive


class Allowance(CaimexCase):
    settings = {"CAIMEX_FREE_ALLOWANCE_USD": "5"}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Setting up the hub started the every-minute check. These tests run
        # each pass themselves, so stop it rather than race it.
        if self.ns["_cx_cap"]["task"] is not None:
            self.ns["_cx_cap"]["task"].cancel()
            self.ns["_cx_cap"]["task"] = None
        self.ns["_cx_grants"].create(self.db.get_bind(), checkfirst=True)

    async def sweep(self):
        return await self.ns["_cx_cap_sweep"](self.db)

    def member(self, email):
        return next(m for m in self.caimex.members if m["email"] == email)

    def granted(self):
        return sorted((r.caimex_user_id, r.amount) for r in
                      self.db.execute(self.ns["_cx_grants"].select()).fetchall())

    async def test_an_uncapped_member_gets_a_fresh_five_dollars_for_life(self):
        self.caimex.add_member("ann@zai.example", spend_used="3.10")     # already spent some
        self.assertEqual(await self.sweep(), 1)
        ann = self.member("ann@zai.example")
        self.assertEqual((ann["spend_limit"], ann["spend_period"], ann["spend_used"]),
                         ("5.00", "lifetime", "0"))
        self.assertIn(("cap", ("ann@zai.example", {
            "spend_limit": "5.00", "spend_period": "lifetime", "reset_spend_used": True})),
            self.caimex.calls)
        self.assertEqual(self.granted(), [("user:ann@zai.example", "5.00")])

    async def test_owners_and_admins_are_never_capped(self):
        self.caimex.add_member("owner@zai.example", role="owner")
        self.caimex.add_member("boss@zai.example", role="admin")
        self.assertEqual(await self.sweep(), 0)
        self.assertIsNone(self.member("owner@zai.example")["spend_limit"])
        self.assertIsNone(self.member("boss@zai.example")["spend_limit"])

    async def test_other_staff_roles_are_capped(self):
        self.caimex.add_member("people@zai.example", role="people_admin")
        self.caimex.add_member("money@zai.example", role="billing_admin")
        self.assertEqual(await self.sweep(), 2)

    async def test_a_cap_already_there_is_left_alone(self):
        self.caimex.add_member("vip@zai.example", spend_limit="1000.00", spend_used="12.00")
        self.assertEqual(await self.sweep(), 0)
        vip = self.member("vip@zai.example")
        self.assertEqual((vip["spend_limit"], vip["spend_used"]), ("1000.00", "12.00"))

    async def test_the_allowance_is_given_once(self):
        self.caimex.add_member("ann@zai.example")
        await self.sweep()
        self.caimex.members.clear()                                       # she leaves...
        self.caimex.add_member("ann@zai.example", user_id="user:ann@zai.example")  # ...and rejoins
        self.caimex.calls.clear()
        self.assertEqual(await self.sweep(), 1)
        self.assertEqual(self.member("ann@zai.example")["spend_limit"], "0.00")
        self.assertIn(("cap", ("ann@zai.example", {"spend_limit": "0.00", "spend_period": "lifetime"})),
                      self.caimex.calls)
        self.assertEqual(self.granted(), [("user:ann@zai.example", "5.00")])

    async def test_a_second_pass_changes_nothing(self):
        self.caimex.add_member("ann@zai.example")
        await self.sweep()
        self.caimex.calls.clear()
        self.assertEqual(await self.sweep(), 0)
        self.assertEqual([c[0] for c in self.caimex.calls], ["members"])

    async def test_without_admin_rights_nobody_is_recorded_and_it_retries(self):
        self.caimex.add_member("ann@zai.example")
        self.caimex.can_set_caps = False
        with self.assertLogs(level="WARNING") as logs:
            self.assertEqual(await self.sweep(), 0)
        self.assertIn("could not cap ann@zai.example: HTTP 403", "\n".join(logs.output))
        self.assertEqual(self.granted(), [])
        self.caimex.can_set_caps = True
        self.assertEqual(await self.sweep(), 1)
        self.assertEqual(self.member("ann@zai.example")["spend_limit"], "5.00")

    async def test_one_failure_does_not_stop_the_others(self):
        bad = self.caimex.add_member("bad@zai.example")
        self.caimex.add_member("good@zai.example")
        self.caimex.cap_failures.add(bad["id"])
        with self.assertLogs(level="WARNING"):
            self.assertEqual(await self.sweep(), 1)
        self.assertEqual(self.member("good@zai.example")["spend_limit"], "5.00")
        self.assertEqual(self.granted(), [("user:good@zai.example", "5.00")])

    async def test_an_unreachable_or_refused_caimex_caps_nobody_and_raises_nothing(self):
        self.caimex.add_member("ann@zai.example")
        self.caimex.service_login_ok = False
        with self.assertLogs(level="WARNING") as logs:
            self.assertEqual(await self.sweep(), 0)
        self.assertIn("could not list members", "\n".join(logs.output))
        self.ns["_CX_URL"] = "http://127.0.0.1:1"
        self.ns["_cx"]["login_failed"] = None
        with self.assertLogs(level="WARNING"):
            self.assertEqual(await self.sweep(), 0)

    async def test_a_new_compute_user_is_capped_at_once_not_at_the_next_tick(self):
        self.ns["_CX_SWEEP_SECONDS"] = 3600        # the regular tick will not come
        task = asyncio.ensure_future(self.ns["_cx_cap_forever"](self.db))
        try:
            for _ in range(100):                   # the first, empty, pass
                await asyncio.sleep(0.02)
                if any(c[0] == "members" for c in self.caimex.calls):
                    break
            self.add_user("tendai", "tendai-password-1")
            await self.sign_in("tendai", "tendai-password-1")
            for _ in range(100):
                await asyncio.sleep(0.02)
                if self.member("tendai@zai.example")["spend_limit"] is not None:
                    break
            self.assertEqual(self.member("tendai@zai.example")["spend_limit"], "5.00")
        finally:
            task.cancel()

    async def test_the_handlers_start_the_check_once(self):
        started = []
        self.ns["_cx_cap_forever"] = lambda db: started.append(db) or asyncio.sleep(0)
        self.ns["_cx_cap"]["task"] = None
        NativeAuthenticator.get_handlers(self.auth, types.SimpleNamespace(db=self.db))
        NativeAuthenticator.get_handlers(self.auth, types.SimpleNamespace(db=self.db))
        self.assertEqual(len(started), 1)

    def test_users_are_told_on_the_login_and_signup_pages(self):
        self.assertEqual(self.page_vars["caimex_allowance"], "5.00")
        for name in ("native-login.html", "signup.html"):
            page = template(name)
            self.assertIn("caimex_allowance is defined and caimex_allowance", page, name)
            self.assertIn("personal", page, name)


class AllowanceOff(CaimexCase):
    def test_off_unless_an_amount_is_set(self):
        self.assertIsNone(self.ns["_CX_ALLOWANCE"])
        self.assertIsNone(self.page_vars["caimex_allowance"])

    async def test_off_means_no_check_runs(self):
        self.ns["_cx_cap"]["task"] = None
        NativeAuthenticator.get_handlers(self.auth, types.SimpleNamespace(db=self.db))
        self.assertIsNone(self.ns["_cx_cap"]["task"])


class SweepInterval(CaimexCase):
    def test_a_bad_interval_falls_back_to_a_minute_and_the_hub_still_starts(self):
        os.environ["CAIMEX_ALLOWANCE_SWEEP_SECONDS"] = "1m"
        with self.assertLogs(level="WARNING") as logs:
            importlib.reload(zai_caimex).configure(Config())     # must not raise
        self.assertEqual(zai_caimex._CX_SWEEP_SECONDS, 60.0)
        self.assertIn("is not a number; using 60", "\n".join(logs.output))
        os.environ.pop("CAIMEX_ALLOWANCE_SWEEP_SECONDS", None)


class Clearing(CaimexCase):
    settings = {"CAIMEX_FREE_ALLOWANCE_USD": "5"}

    async def test_clearing_a_cap_after_the_allowance_gives_zero_not_unlimited(self):
        if self.ns["_cx_cap"]["task"] is not None:
            self.ns["_cx_cap"]["task"].cancel()
        self.ns["_cx_grants"].create(self.db.get_bind(), checkfirst=True)
        ann = self.caimex.add_member("ann@zai.example")
        await self.ns["_cx_cap_sweep"](self.db)
        ann["spend_limit"] = None                  # an admin clears it in Caimex
        await self.ns["_cx_cap_sweep"](self.db)
        self.assertEqual(ann["spend_limit"], "0.00")


class AllowanceInvalid(CaimexCase):
    def test_a_bad_amount_turns_it_off_and_says_so(self):
        for raw in ("five", "0", "-5", "5000", "0.004", "nan"):
            with self.subTest(raw=raw):
                os.environ["CAIMEX_FREE_ALLOWANCE_USD"] = raw
                with self.assertLogs(level="ERROR") as logs:
                    importlib.reload(zai_caimex).configure(Config())
                self.assertIn("caimex allowance is OFF", "\n".join(logs.output))
                self.assertIsNone(zai_caimex._CX_ALLOWANCE)
        os.environ.pop("CAIMEX_FREE_ALLOWANCE_USD", None)


class Wiring(unittest.TestCase):
    def test_every_setting_is_an_optional_secret(self):
        for name in SETTINGS:
            ref = VALUES["hub"]["extraEnv"][name]["valueFrom"]["secretKeyRef"]
            self.assertEqual(ref["name"], "caimex-provision")
            self.assertTrue(ref["optional"], name)

    def test_deploy_script_writes_the_keys_the_hub_reads(self):
        deploy = open(os.path.join(REPO, "scripts", "05-jupyterhub.sh")).read()
        for name in SETTINGS:
            key = VALUES["hub"]["extraEnv"][name]["valueFrom"]["secretKeyRef"]["key"]
            self.assertIn(f"--from-literal={key}=", deploy)
            self.assertIn("${%s" % name, deploy.replace('"$%s"' % name, "${%s}" % name))

    def test_the_hub_loads_the_module_and_ships_it(self):
        startup = VALUES["hub"]["extraConfig"]["03-zai-platform"]
        self.assertIn("import zai_caimex", startup)
        self.assertIn("zai_caimex.configure(c)", startup)
        deploy = open(os.path.join(REPO, "scripts", "05-jupyterhub.sh")).read()
        self.assertIn("--from-file=hub-ext/zai_caimex.py", deploy)
        checksum = deploy.split("ZAI_SUM=")[1].split("\n")[0]
        self.assertIn("hub-ext/zai_caimex.py", checksum)   # a change restarts the hub

    def test_the_allowance_ships_at_five_dollars(self):
        self.assertEqual(VALUES["hub"]["extraEnv"]["CAIMEX_FREE_ALLOWANCE_USD"], "5")

    def test_env_example_lists_every_setting(self):
        example = open(os.path.join(REPO, ".env.example")).read()
        for name in SETTINGS:
            self.assertIn(name + "=", example)


if __name__ == "__main__":
    unittest.main()
