"""Tests for hub-ext/zai_payments.py: online payment for credit.

The module runs against the real zai_platform and zai_credit, a throwaway
ledger, and a stand-in for Econet's Eco Payments API (no network). It needs
the hub's packages:

    pip install jupyterhub jupyterhub-nativeauthenticator==1.2.0 jupyterhub-kubespawner pyyaml
    cd hub-ext && python3 -m unittest -v test_zai_payments
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import types
import unittest

import yaml
from jinja2 import DictLoader, Environment
from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application
from traitlets.config import Config

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
VALUES = yaml.safe_load(open(os.path.join(REPO, "charts", "jupyterhub-values.yaml")))
MICROS = 1_000_000

# Fresh copies on a stand-in for KubeSpawner and a throwaway database; other
# test files' modules are put back after.
os.environ["ZAI_DB"] = os.path.join(tempfile.mkdtemp(), "usage.sqlite")
os.environ["ZAI_CATALOG"] = os.path.join(HERE, "catalog.yaml")
_NAMES = ("zai_platform", "kubespawner", "zai_credit", "zai_payments")
_SAVED = {k: sys.modules.pop(k, None) for k in _NAMES}
sys.modules["kubespawner"] = types.SimpleNamespace(KubeSpawner=object)
import zai_platform  # noqa: E402  (hub-ext is this file's own directory)
import zai_credit  # noqa: E402
import zai_payments  # noqa: E402
for _k, _v in _SAVED.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v

CREDENTIALS = {"ECO_PAYMENTS_CLIENT_ID": "zai-app", "ECO_PAYMENTS_CLIENT_SECRET": "s3cret",
               "ECO_PAYMENTS_PUBLIC_URL": "https://compute.example"}


class Gateway:
    """A stand-in for the Eco Payments API: records calls, answers from a script."""

    def __init__(self):
        self.calls = []
        self.create = (201, {"paymentId": "sasai_p1", "status": "PENDING_AUTHORIZATION",
                             "nextAction": {"type": "REDIRECT", "redirectUrl": "https://pay.sasai.example/c/1"}})
        self.status = {}          # paymentId -> status, or (code, body)

    async def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST" and path == "/payments":
            return self.create
        if method == "GET" and path.startswith("/payments/"):
            pid = path[len("/payments/"):]
            answer = self.status.get(pid, "PENDING_AUTHORIZATION")
            if isinstance(answer, tuple):
                return answer
            return 200, {"paymentId": pid, "status": answer}
        if method == "GET" and path.startswith("/payment-methods"):
            return 200, ["SASAI_WALLET", "CARD", "ECOCASH"]
        if path == "/webhooks":
            return (200, []) if method == "GET" else (201, {"webhookId": "wh_1"})
        return 404, {}

    def gets(self):
        return [c for c in self.calls if c[0] == "GET" and c[1].startswith("/payments/")]


class Case(unittest.IsolatedAsyncioTestCase):
    env = CREDENTIALS

    def setUp(self):
        if os.path.exists(zai_platform.DB_PATH):
            os.remove(zai_platform.DB_PATH)
        zai_platform.CATALOG = zai_platform.Catalog.load()
        zai_platform.init_db()
        self.config = Config()
        self.config.JupyterHub.template_vars = {}
        zai_credit.configure(self.config)
        zai_payments._pay_checked.clear()
        zai_payments.configure(self.config, env=dict(self.env))
        self.gateway = Gateway()
        self._real_call = zai_payments._pay_call
        zai_payments._pay_call = self.gateway

    def tearDown(self):
        zai_payments._pay_call = self._real_call
        task = zai_payments._PAY.get("task")
        if task is not None:
            task.cancel()
        zai_payments._PAY["task"] = None

    async def start(self, username="tendai", amount=25, method="SASAI_WALLET", mobile=""):
        return await zai_payments._pay_start(username, f"{username}@zai.example", amount * MICROS,
                                             method, mobile, "https://compute.example/hub/credit/pay/return")

    def balance(self, username="tendai"):
        with zai_platform._db() as con:
            return zai_platform.account(con, username)["balance"]

    def request(self, username="tendai"):
        with zai_platform._db() as con:
            r = con.execute("SELECT * FROM zai_credit_requests WHERE username=? ORDER BY id DESC LIMIT 1",
                            (username,)).fetchone()
        return dict(r) if r else None

    def payment(self, pid="sasai_p1"):
        with zai_platform._db() as con:
            p = con.execute("SELECT * FROM zai_payments WHERE payment_id=?", (pid,)).fetchone()
        return dict(p) if p else None

    def ledger(self, username="tendai"):
        with zai_platform._db() as con:
            return [dict(r) for r in con.execute("SELECT * FROM ledger WHERE username=?", (username,))]


class Settings(Case):
    async def test_off_without_both_credentials(self):
        for env in ({}, {"ECO_PAYMENTS_CLIENT_ID": "x"}, {"ECO_PAYMENTS_CLIENT_SECRET": "y"}):
            zai_payments.configure(Config(), env=env)
            self.assertFalse(zai_payments._PAY["on"], env)
            self.assertIsNone(zai_credit._CR_ONLINE["start"])
            self.assertEqual(zai_credit._CR_ONLINE["methods"], [])

    async def test_on_with_credentials_offers_the_methods(self):
        self.assertTrue(zai_payments._PAY["on"])
        self.assertEqual(zai_payments._PAY["url"], zai_payments._PAY_DEFAULT_URL)
        self.assertEqual([v for v, _ in zai_credit._CR_ONLINE["methods"]],
                         ["online:SASAI_WALLET", "online:CARD", "online:ECOCASH"])

    async def test_the_api_must_be_https(self):
        zai_payments.configure(Config(), env=dict(CREDENTIALS, ECO_PAYMENTS_URL="http://api.example/v1"))
        self.assertFalse(zai_payments._PAY["on"])
        zai_payments.configure(Config(), env=dict(CREDENTIALS, ECO_PAYMENTS_URL="http://127.0.0.1:9/v1"))
        self.assertTrue(zai_payments._PAY["on"])          # a local stand-in, for testing

    async def test_a_plain_http_public_url_is_ignored(self):
        zai_payments.configure(Config(), env=dict(CREDENTIALS, ECO_PAYMENTS_PUBLIC_URL="http://compute.example"))
        self.assertEqual(zai_payments._PAY["public"], "")

    async def test_poll_interval_is_kept_sensible(self):
        for given, kept in (("1", 15.0), ("90", 90.0), ("99999", 3600.0), ("soon", 60.0)):
            zai_payments.configure(Config(), env=dict(CREDENTIALS, ECO_PAYMENTS_POLL_SECONDS=given))
            self.assertEqual(zai_payments._PAY["poll"], kept, given)

    async def test_methods_answer_in_any_shape(self):
        f = zai_payments._pay_methods_from
        # The shape the live API answers with (October 2026).
        live = {"methods": [{"method": "CARD", "available": True, "displayName": "Bank card"},
                            {"method": "ECOCASH", "available": True, "displayName": "EcoCash wallet"},
                            {"method": "SASAI_WALLET", "available": False, "displayName": "Sasai"}]}
        self.assertEqual(f(live), [("CARD", "Bank card"), ("ECOCASH", "EcoCash wallet")])
        self.assertEqual(f(["CARD", "ECOCASH"]), [("CARD", "Card"), ("ECOCASH", "EcoCash")])
        self.assertEqual(f([{"paymentMethod": "card"}, {"code": "SASAI_WALLET"}, {"code": "BITCOIN"}]),
                         [("CARD", "Card"), ("SASAI_WALLET", "Sasai wallet")])
        self.assertEqual(f({"paymentMethods": ["ECOCASH", "ECOCASH"]}), [("ECOCASH", "EcoCash")])
        self.assertEqual(f({"data": "nonsense"}), [])
        self.assertEqual(f(None), [])

    async def test_live_methods_answer_sets_the_form(self):
        async def live(method, path, body=None):
            return 200, {"methods": [{"method": "CARD", "available": True, "displayName": "Bank card"},
                                     {"method": "ECOCASH", "available": True, "displayName": "EcoCash wallet"}]}
        zai_payments._pay_call = live
        await zai_payments._pay_refresh_methods()
        self.assertEqual(zai_credit._CR_ONLINE["methods"],
                         [("online:CARD", "Bank card"), ("online:ECOCASH", "EcoCash wallet")])
        with self.assertRaises(ValueError):
            await self.start(method="SASAI_WALLET")       # not offered to ZAI

    async def test_refreshing_methods_narrows_the_form(self):
        async def only_card(method, path, body=None):
            return 200, {"paymentMethods": [{"paymentMethod": "CARD"}]}
        zai_payments._pay_call = only_card
        await zai_payments._pay_refresh_methods()
        self.assertEqual(zai_credit._CR_ONLINE["methods"], [("online:CARD", "Card")])  # no displayName
        with self.assertRaises(ValueError):
            await self.start(method="ECOCASH", mobile="+263771234567")

    async def test_webhook_registered_once(self):
        await zai_payments._pay_register_webhook()
        posts = [c for c in self.gateway.calls if c[0] == "POST" and c[1] == "/webhooks"]
        self.assertEqual(posts[0][2], {"url": "https://compute.example/hub/api/zai/payments/webhook",
                                       "events": ["payment.succeeded", "payment.failed"]})


class Starting(Case):
    async def test_creates_the_payment_and_sends_the_payer_to_checkout(self):
        url = await self.start()
        self.assertEqual(url, "https://pay.sasai.example/c/1")
        method, path, body = self.gateway.calls[-1]
        self.assertEqual((method, path), ("POST", "/payments"))
        self.assertTrue(body["merchantReference"].startswith("zai-credit-"))
        self.assertEqual((body["paymentMethod"], body["amount"], body["currency"]), ("SASAI_WALLET", 25.0, "USD"))
        self.assertEqual(body["payer"], {"customerId": "tendai", "email": "tendai@zai.example"})
        self.assertEqual(body["returnUrl"], "https://compute.example/hub/credit/pay/return")
        r, p = self.request(), self.payment()
        self.assertEqual((r["status"], r["method"], r["reference"]), ("pending", "online", "sasai_p1"))
        self.assertEqual((p["status"], p["next_action"], p["credited"], p["micros"]),
                         ("PENDING_AUTHORIZATION", "REDIRECT", 0, 25 * MICROS))
        self.assertEqual(self.balance(), 0)

    async def test_live_checkout_shape(self):
        # What the live API answered on 25 October 2026.
        self.gateway.create = (201, {"paymentId": "sasai_p1", "status": "PENDING_AUTHORIZATION",
                                     "nextAction": {"type": "CHECKOUT", "checkout": {
                                         "url": "https://checkout.sasaipaymentgateway.com/cui/checkout?checkoutCode=x"}}})
        self.assertEqual(await self.start(method="CARD"),
                         "https://checkout.sasaipaymentgateway.com/cui/checkout?checkoutCode=x")
        self.assertEqual(self.payment()["next_action"], "CHECKOUT")

    async def test_a_missing_checkout_address_is_filled_in_on_the_next_check(self):
        self.gateway.create = (201, {"paymentId": "sasai_p1", "status": "PENDING_AUTHORIZATION",
                                     "nextAction": {"type": "CHECKOUT"}})
        await self.start()
        self.assertEqual(self.payment()["redirect"], "")
        self.gateway.status["sasai_p1"] = (200, {"paymentId": "sasai_p1", "status": "PENDING_AUTHORIZATION",
                                                 "nextAction": {"type": "CHECKOUT", "checkout": {"url": "https://pay.example/c"}}})
        await zai_payments._pay_check("sasai_p1", force=True)
        self.assertEqual(self.payment()["redirect"], "https://pay.example/c")

    async def test_return_address_only_when_https(self):
        await zai_payments._pay_start("tendai", "", 25 * MICROS, "CARD", "", "http://127.0.0.1:8090/hub/credit/pay/return")
        self.assertNotIn("returnUrl", self.gateway.calls[-1][2])

    async def test_refusal_reason_is_logged_without_credentials(self):
        self.gateway.create = (400, {"code": "VALIDATION_ERROR", "message": "bad zai-app s3cret"})
        with self.assertLogs(level="WARNING") as logs:
            with self.assertRaises(ValueError):
                await self.start()
        text = "\n".join(logs.output)
        self.assertIn("VALIDATION_ERROR bad [hidden] [hidden]", text)
        self.assertNotIn("s3cret", text)

    async def test_each_payment_has_its_own_reference(self):
        await self.start()
        zai_credit._cr_cancel("tendai")
        self.gateway.create = (201, {"paymentId": "sasai_p2", "status": "PENDING_AUTHORIZATION"})
        await self.start()
        refs = [c[2]["merchantReference"] for c in self.gateway.calls if c[1] == "/payments"]
        self.assertEqual(len(set(refs)), 2)

    async def test_a_non_https_checkout_link_is_not_followed(self):
        self.gateway.create = (201, {"paymentId": "sasai_p1", "status": "PENDING_AUTHORIZATION",
                                     "nextAction": {"type": "REDIRECT", "redirectUrl": "javascript:alert(1)"}})
        self.assertEqual(await self.start(), "")
        self.assertEqual(self.payment()["redirect"], "")

    async def test_ecocash_needs_a_zimbabwe_number(self):
        with self.assertRaises(ValueError):
            await self.start(method="ECOCASH")
        with self.assertRaises(ValueError):
            await self.start(method="ECOCASH", mobile="0771234567")
        self.assertIsNone(self.request())                 # nothing stored for a refused form
        self.gateway.create = (201, {"paymentId": "sasai_p1", "status": "PENDING_AUTHORIZATION",
                                     "nextAction": {"type": "USSD_PUSH"}})
        self.assertEqual(await self.start(method="ECOCASH", mobile="+263 77 123 4567"), "")
        self.assertEqual(self.gateway.calls[-1][2]["payer"]["mobile"], "+263771234567")
        self.assertEqual(self.payment()["next_action"], "USSD_PUSH")

    async def test_unknown_method_refused(self):
        with self.assertRaises(ValueError):
            await self.start(method="BITCOIN")

    async def test_a_refused_payment_leaves_nothing_waiting(self):
        self.gateway.create = (422, {"message": "invalid"})
        with self.assertRaises(ValueError):
            await self.start()
        r = self.request()
        self.assertEqual((r["status"], r["decided_by"]), ("cancelled", "eco-payments"))
        self.assertIsNone(self.payment())
        self.gateway.create = (201, {"paymentId": "sasai_p1", "status": "PENDING_AUTHORIZATION"})
        await self.start()                                # can try again at once

    async def test_unreachable_provider(self):
        self.gateway.create = (599, {})
        with self.assertRaises(ValueError):
            await self.start()
        self.assertEqual(self.request()["status"], "cancelled")

    async def test_one_waiting_request_at_a_time(self):
        await self.start()
        with self.assertRaises(ValueError):
            await self.start()
        self.assertEqual(len([c for c in self.gateway.calls if c[1] == "/payments"]), 1)


class Settling(Case):
    async def test_success_credits_once_and_approves(self):
        await self.start()
        self.gateway.status["sasai_p1"] = "SUCCEEDED"
        self.assertEqual(await zai_payments._pay_check("sasai_p1", force=True), "SUCCEEDED")
        self.assertEqual(await zai_payments._pay_check("sasai_p1", force=True), "SUCCEEDED")
        self.assertEqual(self.balance(), 25 * MICROS)
        [row] = self.ledger()
        self.assertEqual((row["kind"], row["actor"]), ("topup", "eco-payments"))
        self.assertIn("sasai_p1", row["note"])
        r, p = self.request(), self.payment()
        self.assertEqual((r["status"], r["granted"], r["decided_by"], r["ledger_id"]),
                         ("approved", 25 * MICROS, "eco-payments", row["id"]))
        self.assertEqual((p["credited"], p["ledger_id"]), (1, row["id"]))

    async def test_reported_amount_must_match(self):
        await self.start()
        self.gateway.status["sasai_p1"] = (200, {"paymentId": "sasai_p1", "status": "SUCCEEDED", "amount": 24})
        self.assertEqual(await zai_payments._pay_check("sasai_p1", force=True), "AMOUNT_MISMATCH")
        self.assertEqual(self.balance(), 0)
        self.assertEqual(self.request()["status"], "pending")
        await zai_payments._pay_sweep()                   # no longer swept
        self.assertEqual(len(self.gateway.gets()), 1)

    async def test_matching_amount_is_credited(self):
        await self.start()
        self.gateway.status["sasai_p1"] = (200, {"paymentId": "sasai_p1", "status": "SUCCEEDED", "amount": 25})
        self.assertEqual(await zai_payments._pay_check("sasai_p1", force=True), "SUCCEEDED")
        self.assertEqual(self.balance(), 25 * MICROS)

    async def test_racing_checks_credit_once(self):
        await self.start()
        self.gateway.status["sasai_p1"] = "SUCCEEDED"
        await asyncio.gather(*[zai_payments._pay_check("sasai_p1", force=True) for _ in range(5)])
        self.assertEqual(len(self.ledger()), 1)

    async def test_paid_after_cancelling_is_still_credited(self):
        await self.start()
        zai_credit._cr_cancel("tendai")
        self.gateway.status["sasai_p1"] = "SUCCEEDED"
        await zai_payments._pay_check("sasai_p1", force=True)
        self.assertEqual(self.balance(), 25 * MICROS)
        self.assertEqual(self.request()["status"], "approved")

    async def test_failed_and_expired_let_the_user_try_again(self):
        for status in ("FAILED", "EXPIRED"):
            with self.subTest(status):
                self.gateway.create = (201, {"paymentId": f"sasai_{status}", "status": "PENDING_AUTHORIZATION"})
                await self.start()
                self.gateway.status[f"sasai_{status}"] = status
                await zai_payments._pay_check(f"sasai_{status}", force=True)
                r = self.request()
                self.assertEqual((r["status"], r["decided_by"]), ("declined", "eco-payments"))
                self.assertIn("try again", r["reason"])
        self.assertEqual(self.balance(), 0)

    async def test_still_pending_changes_nothing(self):
        await self.start()
        await zai_payments._pay_check("sasai_p1", force=True)
        self.assertEqual(self.request()["status"], "pending")
        self.assertEqual(self.balance(), 0)

    async def test_bad_or_missing_answers_change_nothing(self):
        await self.start()
        for answer in ((599, {}), (404, {}), (500, {"status": "SUCCEEDED"}),
                       (200, {"paymentId": "sasai_other", "status": "SUCCEEDED"})):
            self.gateway.status["sasai_p1"] = answer
            await zai_payments._pay_check("sasai_p1", force=True)
        self.assertEqual((self.payment()["status"], self.balance()), ("PENDING_AUTHORIZATION", 0))

    async def test_the_payment_id_is_quoted_in_the_path(self):
        self.gateway.create = (201, {"paymentId": "sasai/../x?y", "status": "PENDING_AUTHORIZATION"})
        await self.start()
        await zai_payments._pay_check("sasai/../x?y", force=True)
        self.assertEqual(self.gateway.gets()[-1][1], "/payments/sasai%2F..%2Fx%3Fy")

    async def test_checks_of_one_payment_are_spaced_out(self):
        await self.start()
        await zai_payments._pay_check("sasai_p1")
        await zai_payments._pay_check("sasai_p1")
        self.assertEqual(len(self.gateway.gets()), 1)
        await zai_payments._pay_check("sasai_p1", force=True)
        self.assertEqual(len(self.gateway.gets()), 2)

    async def test_unknown_payment(self):
        self.assertIsNone(await zai_payments._pay_check("sasai_nope", force=True))

    async def test_sweep_checks_open_payments_and_gives_up_after_a_week(self):
        await self.start("tendai")
        self.gateway.create = (201, {"paymentId": "sasai_old", "status": "PENDING_AUTHORIZATION"})
        await self.start("rudo")
        with zai_platform._db() as con:
            con.execute("UPDATE zai_payments SET created=? WHERE payment_id='sasai_old'", (time.time() - 8 * 86400,))
        self.gateway.status["sasai_p1"] = "SUCCEEDED"
        await zai_payments._pay_sweep()
        self.assertEqual(self.balance("tendai"), 25 * MICROS)
        self.assertEqual(self.payment("sasai_old")["status"], "ABANDONED")
        self.assertEqual(self.request("rudo")["status"], "cancelled")
        self.assertEqual([c[1] for c in self.gateway.gets()], ["/payments/sasai_p1"])

    async def test_admins_cannot_settle_online_payments_by_hand(self):
        await self.start()
        rid = self.request()["id"]
        for action in ("approve", "decline"):
            with self.assertRaises(ValueError):
                zai_credit._cr_decide(rid, "admin", action, amount="25", reason="x")
        self.assertEqual(self.balance(), 0)


class Webhook(AsyncHTTPTestCase):
    """Unauthenticated: it may only make us look, never change anything itself."""

    def get_app(self):
        return Application([(r"/wh", zai_payments.PayWebhookHandler)])

    def setUp(self):
        super().setUp()
        if os.path.exists(zai_platform.DB_PATH):
            os.remove(zai_platform.DB_PATH)
        zai_platform.CATALOG = zai_platform.Catalog.load()
        zai_platform.init_db()
        zai_credit._cr_init()
        zai_payments._pay_init()
        zai_payments._PAY["on"] = True
        self.looked = []
        self._real = zai_payments._pay_check

        async def look(pid, force=False):
            self.looked.append(pid)
        zai_payments._pay_check = look
        rid = zai_credit._cr_create("tendai", "", 25 * MICROS, "online", "", "")
        now = time.time()
        with zai_platform._db() as con:
            con.execute("INSERT INTO zai_payments(request_id, username, micros, method, reference, payment_id,"
                        " status, created, updated) VALUES (?,?,?,?,?,?,?,?,?)",
                        (rid, "tendai", 25 * MICROS, "CARD", "zai-credit-1", "sasai_p1",
                         "PENDING_AUTHORIZATION", now, now))

    def tearDown(self):
        zai_payments._pay_check = self._real
        super().tearDown()

    def post(self, body):
        r = self.fetch("/wh", method="POST", body=body if isinstance(body, str) else json.dumps(body))
        self.io_loop.run_sync(lambda: asyncio.sleep(0.05))
        return r

    def test_known_payment_is_checked_not_believed(self):
        r = self.post({"event": "payment.succeeded", "data": {"paymentId": "sasai_p1", "status": "SUCCEEDED"}})
        self.assertEqual(r.code, 200)
        self.assertEqual(self.looked, ["sasai_p1"])
        with zai_platform._db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM ledger").fetchone()[0], 0)

    def test_unknown_or_garbage_is_ignored_quietly(self):
        for body in ({"paymentId": "sasai_unknown", "status": "SUCCEEDED"}, "not json", {}, [1, 2]):
            self.assertEqual(self.post(body).code, 200)
        self.assertEqual(self.looked, [])

    def test_ignored_when_payments_are_off(self):
        zai_payments._PAY["on"] = False
        self.post({"paymentId": "sasai_p1"})
        self.assertEqual(self.looked, [])


class Pages(Case):
    def render(self, name, user, **variables):
        env = Environment(autoescape=True, loader=DictLoader({
            "page.html": "{% block main %}{% endblock %}{% block script %}{% endblock %}",
            name: open(os.path.join(REPO, "templates", name)).read()}))
        cat = zai_platform.CATALOG
        return env.get_template(name).render(
            base_url="/hub/", xsrf="X", user=user, catalog=cat, money=cat.money, rate_str=cat.rate_str,
            when=zai_platform._when, dur=zai_platform._dur, size_name=zai_platform._size_name,
            zai_credit=zai_credit._cr_view(user), zai_pay=zai_payments._pay_view(user), **variables)

    def account(self):
        user = types.SimpleNamespace(name="tendai", admin=False, kind="user", running=False, url="/user/tendai/")
        with zai_platform._db() as con:
            acct = zai_platform.account(con, "tendai")
        return self.render("account.html", user, acct=acct, history=[], free={}, admin_free=False,
                           public_host="h", now=time.time(), two_factor=False)

    async def test_form_offers_pay_now_beside_the_manual_ways(self):
        page = self.account()
        self.assertIn('<optgroup label="Pay now online">', page)
        self.assertIn('<option value="online:SASAI_WALLET">Sasai wallet</option>', page)
        self.assertIn('<option value="bank">Bank transfer, details by email</option>', page)
        self.assertIn('name="mobile"', page)
        self.assertIn(">Pay now</button>", page)

    async def test_form_without_payments_is_unchanged(self):
        zai_payments.configure(Config(), env={})
        page = self.account()
        self.assertNotIn("optgroup", page)
        self.assertNotIn('name="mobile"', page)
        self.assertIn(">Send request</button>", page)

    async def test_waiting_payment(self):
        await self.start()
        page = self.account()
        self.assertIn("is waiting to be confirmed", page)
        self.assertIn('href="https://pay.sasai.example/c/1"', page)
        self.assertIn('action="/hub/credit/pay/check"', page)
        self.assertIn('action="/hub/credit/cancel"', page)
        self.assertNotIn('action="/hub/credit/request"', page)

    async def test_paid_and_failed(self):
        await self.start()
        self.gateway.status["sasai_p1"] = "SUCCEEDED"
        await zai_payments._pay_check("sasai_p1", force=True)
        self.assertIn("Your payment went through", self.account())
        self.gateway.create = (201, {"paymentId": "sasai_p2", "status": "PENDING_AUTHORIZATION"})
        await self.start()
        self.gateway.status["sasai_p2"] = "FAILED"
        await zai_payments._pay_check("sasai_p2", force=True)
        self.assertIn("didn't complete", self.account())

    async def test_billing_shows_online_payments_without_approve(self):
        await self.start()
        admin = types.SimpleNamespace(name="boss", admin=True, kind="user")
        page = self.render("billing.html", admin, people=[], ledger=[], only="", month_topups=0,
                           month_usage=0, message="", error="")
        self.assertIn("Paid online", page)
        self.assertIn('name="payment" value="sasai_p1"', page)
        self.assertNotIn('value="approve"', page)

    async def test_the_hub_loads_it_after_credit_requests_and_ships_it(self):
        startup = VALUES["hub"]["extraConfig"]["03-zai-platform"]
        self.assertLess(startup.index("zai_credit.configure(c)"), startup.index("zai_payments.configure(c)"))
        deploy = open(os.path.join(REPO, "scripts", "05-jupyterhub.sh")).read()
        self.assertIn("--from-file=hub-ext/zai_payments.py", deploy)
        self.assertIn("hub-ext/zai_payments.py", deploy.split("ZAI_SUM=")[1].split("\n")[0])
        env = VALUES["hub"]["extraEnv"]
        for key in ("ECO_PAYMENTS_URL", "ECO_PAYMENTS_CLIENT_ID", "ECO_PAYMENTS_CLIENT_SECRET", "ECO_PAYMENTS_PUBLIC_URL"):
            self.assertEqual(env[key]["valueFrom"]["secretKeyRef"]["name"], "eco-payments")


if __name__ == "__main__":
    unittest.main()
