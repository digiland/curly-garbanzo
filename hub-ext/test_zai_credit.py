"""Tests for hub-ext/zai_credit.py: credit requests.

The module runs against the real zai_platform, its real catalog and a
throwaway ledger, so an approval is checked by the balance it produces. It
needs the hub's packages:

    pip install jupyterhub jupyterhub-nativeauthenticator==1.2.0 jupyterhub-kubespawner pyyaml
    cd hub-ext && python3 -m unittest -v test_zai_credit
"""
import importlib
import os
import sys
import tempfile
import types
import unittest

import yaml
from jinja2 import DictLoader, Environment
from traitlets.config import Config

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
VALUES = yaml.safe_load(open(os.path.join(REPO, "charts", "jupyterhub-values.yaml")))
MICROS = 1_000_000

# The real zai_platform, built on a stand-in for KubeSpawner (nothing here
# starts a server), reading a ledger in a temporary folder. Other test files
# put their own stand-in zai_platform in sys.modules; it is put back after.
_TMP = tempfile.mkdtemp()
os.environ["ZAI_DB"] = os.path.join(_TMP, "usage.sqlite")
os.environ["ZAI_CATALOG"] = os.path.join(HERE, "catalog.yaml")
_SAVED = {k: sys.modules.pop(k, None) for k in ("zai_platform", "kubespawner")}
sys.modules["kubespawner"] = types.SimpleNamespace(KubeSpawner=object)
import zai_platform  # noqa: E402  (hub-ext is this file's own directory)
import zai_credit  # noqa: E402
for _k, _v in _SAVED.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v


def person(name, admin=False):
    return types.SimpleNamespace(name=name, admin=admin, kind="user")


class Case(unittest.TestCase):
    def setUp(self):
        if os.path.exists(os.environ["ZAI_DB"]):
            os.remove(os.environ["ZAI_DB"])
        zai_platform.CATALOG = zai_platform.Catalog.load()
        zai_platform.init_db()
        self.config = Config()
        self.config.JupyterHub.template_vars = {}
        zai_credit.configure(self.config)

    def ask(self, username="tendai", amount="25", method="ecocash", reference="", note=""):
        micros, method, reference, note = zai_credit._cr_validate(amount, method, reference, note)
        return zai_credit._cr_create(username, f"{username}@zai.example", micros, method, reference, note)

    def balance(self, username="tendai"):
        with zai_platform._db() as con:
            return zai_platform.account(con, username)["balance"]

    def request(self, rid):
        with zai_platform._db() as con:
            return dict(con.execute("SELECT * FROM zai_credit_requests WHERE id=?", (rid,)).fetchone())

    def ledger(self, username="tendai"):
        with zai_platform._db() as con:
            return [dict(r) for r in con.execute("SELECT * FROM ledger WHERE username=?", (username,))]


class Validation(Case):
    def test_presets_and_custom_amounts(self):
        for amount, micros in [("10", 10 * MICROS), ("$25", 25 * MICROS), ("5", 5 * MICROS),
                               ("1000", 1000 * MICROS), ("37.50", 37_500_000)]:
            self.assertEqual(zai_credit._cr_validate(amount, "bank", "", "")[0], micros, amount)

    def test_amount_limits_and_format(self):
        for amount in ["4.99", "1000.01", "0", "", "-10", "25.555", "1e3", "twenty", "10,000"]:
            with self.assertRaises(ValueError, msg=amount):
                zai_credit._cr_validate(amount, "bank", "", "")

    def test_method_must_be_on_the_list(self):
        for method in zai_credit._CR_METHODS:
            zai_credit._cr_validate("25", method, "", "")
        with self.assertRaises(ValueError):
            zai_credit._cr_validate("25", "cash", "", "")

    def test_reference_and_note_lengths(self):
        zai_credit._cr_validate("25", "bank", "r" * 100, "n" * 500)
        with self.assertRaises(ValueError):
            zai_credit._cr_validate("25", "bank", "r" * 101, "")
        with self.assertRaises(ValueError):
            zai_credit._cr_validate("25", "bank", "", "n" * 501)


class Requests(Case):
    def test_one_waiting_request_at_a_time(self):
        self.ask()
        with self.assertRaises(ValueError):
            self.ask(amount="50")
        self.ask(username="rudo")       # someone else is not affected

    def test_cancel_then_ask_again(self):
        first = self.ask()
        zai_credit._cr_cancel("tendai")
        self.assertEqual(self.request(first)["status"], "cancelled")
        second = self.ask(amount="50")
        self.assertEqual(self.request(second)["status"], "pending")

    def test_a_request_alone_adds_no_credit(self):
        self.ask()
        self.assertEqual(self.balance(), 0)
        self.assertEqual(self.ledger(), [])

    def test_approve_adds_the_credit_as_a_top_up(self):
        rid = self.ask(reference="EC123")
        zai_credit._cr_decide(rid, "admin", "approve", amount="25.00")
        self.assertEqual(self.balance(), 25 * MICROS)
        [row] = self.ledger()
        self.assertEqual((row["kind"], row["actor"]), ("topup", "admin"))
        self.assertEqual(row["note"], f"Credit request #{rid} · EcoCash ref EC123")
        r = self.request(rid)
        self.assertEqual((r["status"], r["granted"], r["decided_by"], r["ledger_id"]),
                         ("approved", 25 * MICROS, "admin", row["id"]))

    def test_admin_can_change_the_amount_and_the_reference(self):
        rid = self.ask(amount="50", reference="")
        zai_credit._cr_decide(rid, "admin", "approve", amount="40", reference="BANK-77")
        self.assertEqual(self.balance(), 40 * MICROS)
        self.assertEqual(self.request(rid)["reference"], "BANK-77")
        self.assertIn("ref BANK-77", self.ledger()[0]["note"])

    def test_cannot_be_paid_twice(self):
        rid = self.ask()
        zai_credit._cr_decide(rid, "admin", "approve", amount="25")
        for action in ("approve", "decline"):
            with self.assertRaises(ValueError):
                zai_credit._cr_decide(rid, "other-admin", action, amount="25", reason="no")
        self.assertEqual(self.balance(), 25 * MICROS)
        self.assertEqual(len(self.ledger()), 1)

    def test_approve_needs_a_positive_amount(self):
        rid = self.ask()
        for amount in ["", "0", "-5", "abc"]:
            with self.assertRaises(ValueError, msg=amount):
                zai_credit._cr_decide(rid, "admin", "approve", amount=amount)
        self.assertEqual(self.request(rid)["status"], "pending")

    def test_approve_refused_for_a_deleted_user(self):
        rid = self.ask()
        with self.assertRaises(ValueError):
            zai_credit._cr_decide(rid, "admin", "approve", amount="25", user_exists=lambda name: False)
        self.assertEqual(self.ledger(), [])
        zai_credit._cr_decide(rid, "admin", "decline", reason="Account removed")

    def test_decline_needs_a_reason_and_adds_nothing(self):
        rid = self.ask()
        with self.assertRaises(ValueError):
            zai_credit._cr_decide(rid, "admin", "decline", reason="  ")
        zai_credit._cr_decide(rid, "admin", "decline", reason="No payment arrived")
        r = self.request(rid)
        self.assertEqual((r["status"], r["reason"]), ("declined", "No payment arrived"))
        self.assertEqual(self.ledger(), [])
        self.ask()                       # and they can ask again

    def test_cancelled_request_cannot_be_approved(self):
        rid = self.ask()
        zai_credit._cr_cancel("tendai")
        with self.assertRaises(ValueError):
            zai_credit._cr_decide(rid, "admin", "approve", amount="25")

    def test_unknown_request_or_action(self):
        with self.assertRaises(ValueError):
            zai_credit._cr_decide(999, "admin", "approve", amount="25")
        rid = self.ask()
        with self.assertRaises(ValueError):
            zai_credit._cr_decide(rid, "admin", "refund")


class View(Case):
    def test_user_sees_their_latest_request_and_no_one_elses(self):
        self.ask(username="rudo")
        self.assertIsNone(zai_credit._cr_view(person("tendai"))["mine"])
        rid = self.ask()
        view = zai_credit._cr_view(person("tendai"))
        self.assertEqual(view["mine"]["id"], rid)
        self.assertEqual((view["pending"], view["pending_count"]), ([], 0))

    def test_admin_sees_every_waiting_request_and_recent_decisions(self):
        a, b = self.ask(), self.ask(username="rudo")
        zai_credit._cr_decide(b, "admin", "decline", reason="duplicate")
        view = zai_credit._cr_view(person("admin", admin=True))
        self.assertEqual([r["id"] for r in view["pending"]], [a])
        self.assertEqual(view["pending_count"], 1)
        self.assertEqual([r["id"] for r in view["recent"]], [b])

    def test_free_admin_is_not_offered_the_form(self):
        self.assertEqual(zai_credit._cr_view(person("admin", admin=True))["free"],
                         zai_platform.CATALOG.admins_free)
        self.assertFalse(zai_credit._cr_view(person("tendai"))["free"])

    def test_the_hours_guide_uses_the_catalog_prices(self):
        view = zai_credit._cr_view(person("tendai"))
        self.assertEqual(view["vat_percent"], 15.5)
        self.assertEqual([z["rate"] for z in view["sizes"]], [1.32, 1.86])   # GPU sizes, cheapest first

    def test_registered_for_templates_and_routes(self):
        self.assertIs(self.config.JupyterHub.template_vars["zai_credit"], zai_credit._cr_view)
        routes = [h[0] for h in self.config.JupyterHub.extra_handlers]
        self.assertEqual(routes, ["/credit/request", "/credit/cancel", "/credit/decide"])

    def test_services_get_nothing(self):
        self.assertIsNone(zai_credit._cr_view(None))
        self.assertIsNone(zai_credit._cr_view(types.SimpleNamespace(name="svc", admin=True, kind="service")))


class Pages(Case):
    def render(self, name, **variables):
        env = Environment(autoescape=True, loader=DictLoader({
            "page.html": "{% block main %}{% endblock %}{% block script %}{% endblock %}",
            name: open(os.path.join(REPO, "templates", name)).read()}))
        cat = zai_platform.CATALOG
        return env.get_template(name).render(
            base_url="/hub/", xsrf="X", catalog=cat, money=cat.money, rate_str=cat.rate_str,
            when=lambda ts: "WHEN", dur=lambda s: "DUR", size_name=lambda s: s, **variables)

    def account(self, user):
        with zai_platform._db() as con:
            acct = zai_platform.account(con, user.name)
        return self.render("account.html", user=user, acct=acct, history=[], free={},
                           admin_free=False, public_host="h", now=0, two_factor=False,
                           zai_credit=zai_credit._cr_view(user))

    def test_account_page_has_every_field_the_server_reads(self):
        page = self.account(person("tendai"))
        for name in ("amount", "method", "reference", "note", "_xsrf"):
            self.assertIn(f'name="{name}"', page)
        for preset in zai_credit._CR_PRESETS:
            self.assertIn(f'data-amount="{preset}"', page)
        for label in zai_credit._CR_METHODS.values():
            self.assertIn(f">{label}</option>", page)
        self.assertIn('action="/hub/credit/request"', page)
        self.assertIn("Any amount from $5 to $1,000.", page)

    def test_waiting_request_replaces_the_form(self):
        self.ask(note="<script>x</script>")
        page = self.account(person("tendai"))
        self.assertIn("is waiting", page)
        self.assertIn('action="/hub/credit/cancel"', page)
        self.assertNotIn('action="/hub/credit/request"', page)

    def test_declined_reason_is_shown_escaped(self):
        rid = self.ask()
        zai_credit._cr_decide(rid, "admin", "decline", reason="<b>no</b> payment")
        page = self.account(person("tendai"))
        self.assertIn("&lt;b&gt;no&lt;/b&gt; payment", page)
        self.assertIn('action="/hub/credit/request"', page)

    def test_account_page_without_the_feature_is_unchanged(self):
        with zai_platform._db() as con:
            acct = zai_platform.account(con, "tendai")
        page = self.render("account.html", user=person("tendai"), acct=acct, history=[], free={},
                           admin_free=False, public_host="h", now=0, two_factor=False)
        self.assertNotIn("credit/request", page)
        self.assertIn("contact an administrator", page)

    def test_billing_page_lists_waiting_requests(self):
        self.ask(reference="<i>EC1</i>")
        page = self.render("billing.html", people=[], ledger=[], only="", month_topups=0, month_usage=0,
                           message="", error="", zai_credit=zai_credit._cr_view(person("admin", admin=True)))
        self.assertIn("1 waiting", page)
        self.assertIn('href="mailto:tendai@zai.example"', page)
        self.assertIn('value="25.00"', page)
        self.assertIn("&lt;i&gt;EC1&lt;/i&gt;", page)
        self.assertEqual(page.count('action="/hub/credit/decide"'), 2)

    def test_the_hub_loads_the_module_after_the_platform_and_ships_it(self):
        startup = VALUES["hub"]["extraConfig"]["03-zai-platform"]
        self.assertLess(startup.index("zai_platform.configure(c)"), startup.index("zai_credit.configure(c)"))
        deploy = open(os.path.join(REPO, "scripts", "05-jupyterhub.sh")).read()
        self.assertIn("--from-file=hub-ext/zai_credit.py", deploy)
        self.assertIn("hub-ext/zai_credit.py", deploy.split("ZAI_SUM=")[1].split("\n")[0])


if __name__ == "__main__":
    unittest.main()
