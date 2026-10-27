"""Tests for the redesigned pages: the start page's size picker
(zai_platform.options_form), the Account overview and the Endpoints page.

They run against the real zai_platform, its real catalog and a throwaway
database, and render the real templates with a stand-in for JupyterHub's
base page. They need the hub's packages:

    pip install jupyterhub jupyterhub-kubespawner pyyaml
    cd hub-ext && python3 -m unittest -v test_zai_pages
"""
import os
import re
import sys
import tempfile
import time
import types
import unittest

from jinja2 import DictLoader, Environment

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MICROS = 1_000_000

# The real zai_platform on a stand-in for KubeSpawner, as in test_zai_credit.
os.environ["ZAI_DB"] = os.path.join(tempfile.mkdtemp(), "usage.sqlite")
os.environ["ZAI_CATALOG"] = os.path.join(HERE, "catalog.yaml")
# Fresh copies, so this file's zai_myusage uses this file's zai_platform (and
# its database) whatever other test files loaded first.
_SAVED = {k: sys.modules.pop(k, None) for k in ("zai_platform", "kubespawner", "zai_myusage")}
sys.modules["kubespawner"] = types.SimpleNamespace(KubeSpawner=object)
import zai_platform  # noqa: E402  (hub-ext is this file's own directory)
import zai_myusage  # noqa: E402
for _k, _v in _SAVED.items():
    if _v is None:
        sys.modules.pop(_k, None)
    else:
        sys.modules[_k] = _v


def person(name="tendai", admin=False, running=False):
    return types.SimpleNamespace(name=name, admin=admin, kind="user", running=running, url=f"/user/{name}/")


class Case(unittest.TestCase):
    def setUp(self):
        if os.path.exists(zai_platform.DB_PATH):
            os.remove(zai_platform.DB_PATH)
        zai_platform.CATALOG = zai_platform.Catalog.load()
        zai_platform.init_db()

    def credit(self, micros, user="tendai"):
        zai_platform.add_credit(user, micros, "topup", "", "admin")

    def render(self, name, **variables):
        env = Environment(autoescape=True, loader=DictLoader({
            "page.html": "{% block main %}{% endblock %}{% block script %}{% endblock %}",
            name: open(os.path.join(REPO, "templates", name)).read()}))
        cat = zai_platform.CATALOG
        return env.get_template(name).render(
            base_url="/hub/", xsrf="X", catalog=cat, money=cat.money, rate_str=cat.rate_str,
            when=zai_platform._when, dur=zai_platform._dur, size_name=zai_platform._size_name, **variables)


class StartPicker(Case):
    def form(self, user=None):
        return zai_platform.options_form(types.SimpleNamespace(user=user or person()))

    def test_sends_the_same_fields_as_before(self):
        html = self.form()
        sizes = re.findall(r'name="profile" value="([^"]+)"', html)
        self.assertEqual(sizes, [z["slug"] for z in zai_platform.CATALOG.sizes])
        tpls = re.findall(r'name="template" value="([^"]+)"', html)
        self.assertEqual(tpls, [t["slug"] for t in zai_platform.CATALOG.templates])
        self.assertEqual(zai_platform.options_from_form({"profile": ["gpu-18"], "template": ["vllm"]}),
                         {"profile": "gpu-18", "template": "vllm"})

    def test_cards_show_price_hardware_and_the_recommended_size(self):
        html = self.form()
        self.assertIn("<b>$1.58</b><span>/ hour incl. VAT</span>", html)
        self.assertIn("<b>$0.06</b><span>/ hour incl. VAT</span>", html)
        self.assertIn("RAM<b>12 GB</b>", html)
        self.assertIn("RAM<b>24 GB</b>", html)
        self.assertEqual(html.count("Recommended"), 1)
        self.assertIn('value="gpu-18" data-templates="pytorch comfyui vllm"', html)

    def test_free_slices_and_a_full_size(self):
        self.assertIn('H200 slices free</span><b>7 of 7</b>', self.form())
        with zai_platform._db() as con:
            for _ in range(7):
                con.execute("INSERT INTO sessions(username, server, started, gpu, resource, gpus)"
                            " VALUES ('x', '', ?, 1, 'nvidia.com/gpu', 1)", (time.time(),))
        html = self.form()
        self.assertIn("<b>0 of 7</b>", html)
        self.assertRegex(html, r'value="gpu-18"[^>]*disabled')
        self.assertIn(">Full<", html)

    def test_a_pool_without_a_label_is_called_gpu_slices(self):
        zai_platform.CATALOG.pool_labels = {}
        self.assertIn('GPU slices free</span>', self.form())

    def test_a_link_from_home_can_pick_the_size(self):
        self.assertIn("location.hash", self.form())

    def test_summary_data_for_the_bar(self):
        self.credit(20 * MICROS)
        html = self.form()
        self.assertIn('data-balance="20000000"', html)
        self.assertIn('data-money="$20.00"', html)
        self.assertIn('data-rate="1580000"', html)
        self.assertIn("z-summary", open(os.path.join(REPO, "templates", "spawn.html")).read())

    def test_free_admin_sees_free(self):
        html = self.form(person("boss", admin=True))
        self.assertIn("<b>Free</b><span>for administrators</span>", html)
        self.assertIn('data-free="1"', html)


class AccountOverview(Case):
    def account(self, user=None, admin_free=False, month=True):
        user = user or person()
        with zai_platform._db() as con:
            acct = zai_platform.account(con, user.name)
            history = [dict(r) for r in con.execute(
                "SELECT * FROM ledger WHERE username=? ORDER BY ts DESC LIMIT 100", (user.name,))]
        extra = {"zai_month": zai_myusage._mu_month(user)} if month else {}
        return self.render("account.html", user=user, acct=acct, history=history, free={},
                           admin_free=admin_free, public_host="h", now=time.time(), two_factor=False, **extra)

    def test_balance_in_hours_of_the_default_size(self):
        self.credit(20 * MICROS)
        page = self.account()
        self.assertIn("$20.00", page)
        self.assertIn("About <strong>13 hours</strong> on GPU · 18 GB", page)       # $20 at $1.58

    def test_running_server_card(self):
        with zai_platform._db() as con:
            con.execute("INSERT INTO sessions(username, server, started, gpu, size, rate)"
                        " VALUES ('tendai', '', ?, 1, 'gpu-18', 1320000)", (time.time() - 1800,))
        page = self.account(person(running=True))
        self.assertIn("GPU · 18 GB", page)
        self.assertIn("30m", page)
        self.assertIn('href="/user/tendai/"', page)
        self.assertNotIn("Nothing running", page)

    def test_nothing_running(self):
        page = self.account()
        self.assertIn("Nothing running", page)
        self.assertIn('href="/hub/spawn"', page)

    def test_history_shows_the_latest_ten(self):
        for i in range(12):
            self.credit((i + 1) * MICROS)
        page = self.account()
        self.assertEqual(page.count('class="amt'), 10)
        self.assertIn("Showing the latest 10.", page)
        self.assertIn("+$12.00", page)
        self.assertNotIn("+$1.00<", page)

    def test_month_chart(self):
        page = self.account()
        self.assertEqual(page.count("<i"), 14 + page.count("<i aria-hidden"))
        self.assertIn('class="is-today"', page)

    def test_without_the_usage_module(self):
        page = self.account(month=False)
        self.assertNotIn("This month", page)
        self.assertIn("Balance", page)


class Home(Case):
    def page(self, user=None, active=False):
        user = user or person(running=active)
        return self.render("home.html", user=user, url="/hub/spawn/" + user.name,
                           default_server=types.SimpleNamespace(active=active, ready=active),
                           zai_home=zai_myusage._mu_home(user))

    def test_stopped_lists_sizes_with_hours_and_links_to_the_picker(self):
        self.credit(20 * MICROS)
        page = self.page()
        self.assertIn('href="/hub/spawn#gpu-18"', page)
        self.assertIn("<b>$1.58</b><span>/ hour incl. VAT</span>", page)
        self.assertIn("covers about <strong>13 hours</strong>", page)
        self.assertIn('id="start"', page)
        self.assertNotIn('id="stop"', page)

    def test_running_shows_the_session_and_keeps_the_stop_button(self):
        with zai_platform._db() as con:
            con.execute("INSERT INTO sessions(username, server, started, gpu, size, rate)"
                        " VALUES ('tendai', '', ?, 1, 'gpu-18', 1320000)", (time.time() - 600,))
        page = self.page(active=True)
        self.assertIn("10m", page)
        self.assertIn("$1.32/h incl. VAT", page)
        self.assertIn('id="stop"', page)
        self.assertIn('id="start"', page)
        self.assertNotIn("spawn#", page)

    def test_free_admin(self):
        page = self.page(person("boss", admin=True))
        self.assertIn("<b>Free</b><span>for administrators</span>", page)
        self.assertNotIn("covers about", page)

    def test_without_the_usage_module_it_is_the_plain_card(self):
        page = self.render("home.html", user=person(), url="/hub/spawn/tendai",
                           default_server=types.SimpleNamespace(active=False, ready=False))
        self.assertIn('id="start"', page)
        self.assertNotIn("spawn#", page)


class Token(Case):
    """JupyterHub's token.js does the work; the page must keep what it uses."""

    def page(self, api_tokens=(), oauth_clients=()):
        return self.render("token.html", user=person(), api_tokens=list(api_tokens),
                           oauth_clients=list(oauth_clients),
                           token_expires_in_options_html='<option value="3600">1 Hour</option>'
                           '<option value="604800">1 Week</option><option value="" selected>Never</option>')

    def token(self, scopes, note="laptop", expires=True):
        from datetime import datetime
        return types.SimpleNamespace(api_id="a1", note=note, scopes=scopes, last_activity=None,
                                     created=datetime(2026, 10, 18), expires_at=datetime(2026, 10, 25) if expires else None)

    def test_keeps_everything_token_js_uses(self):
        page = self.page([self.token(["inherit"])], [{"token_id": "o1", "description": "Server at /user/tendai/",
                                                      "last_activity": None, "created": None}])
        for needle in ('id="request-token-form"', 'id="token-note"', 'id="token-expiration-seconds"',
                       'id="token-scopes"', 'id="token-area"', 'id="token-result"', 'data-token-id="a1"',
                       'data-token-id="o1"', 'class="revoke-token-btn', 'class="time-col"', 'require(["token"])'):
            self.assertIn(needle, page)
        self.assertEqual(page.count('class="revoke-token-btn'), 2)

    def test_full_access_and_limited_tokens(self):
        page = self.page([self.token(["inherit"]), self.token(["servers", "access:servers"], note="job", expires=False)])
        self.assertIn('<span class="z-pill">Everything</span>', page)
        self.assertIn("2 permissions", page)
        self.assertIn("<code>access:servers</code>", page)
        self.assertIn('<span class="z-pill">Never</span>', page)

    def test_empty_lists(self):
        page = self.page()
        self.assertIn("No tokens yet", page)
        self.assertIn("No apps signed in", page)
        self.assertNotIn('id="api-tokens-table"', page)

    def test_note_is_escaped(self):
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", self.page([self.token(["inherit"], note="<b>x</b>")]))

    def test_examples_use_this_sites_address_and_the_user(self):
        page = self.page()
        self.assertIn('/user/tendai/proxy/8000/', page)
        self.assertIn('class="z-origin"', page)


class Endpoints(Case):
    def page(self, unavailable="", endpoints=()):
        return self.render("endpoints.html", user=person(admin=True), endpoints=list(endpoints),
                           unavailable=unavailable, spend={}, message="", error="",
                           public_host="h", users=[], now_ms=0, form={})

    def test_not_set_up_is_coming_soon(self):
        page = self.page(unavailable="Serverless is not set up on this hub.")
        self.assertIn("Coming soon", page)
        self.assertIn("How it will work", page)
        self.assertNotIn("is not set up on this hub", page)
        self.assertNotIn(">Refresh<", page)

    def test_unreachable_stays_an_alert(self):
        page = self.page(unavailable="The serverless service is not reachable right now. Try again in a minute.")
        self.assertNotIn("Coming soon", page)
        self.assertIn("not reachable right now", page)
        self.assertIn(">Refresh<", page)

    def test_working_but_empty(self):
        page = self.page()
        self.assertIn("No endpoints yet", page)
        self.assertNotIn("Coming soon", page)


class Logout(unittest.TestCase):
    """templates/page.html, the base of every page: Logout asks first."""

    def render(self, **variables):
        env = Environment(autoescape=True, loader=DictLoader({
            "templates/page.html": "{% block login_widget %}{% endblock %}{% block script %}{% endblock %}",
            "page.html": open(os.path.join(REPO, "templates", "page.html")).read()}))
        return env.get_template("page.html").render(
            base_url="/hub/", logout_url="/hub/logout", login_url="/hub/login", **variables)

    def test_asks_before_signing_out(self):
        page = self.render(user=types.SimpleNamespace(name="tendai", active=False))
        self.assertIn('id="logout"', page)
        self.assertIn('<dialog id="z-logout"', page)
        self.assertIn("Sign out of ZAI Notebooks?", page)
        self.assertIn('<a class="z-btn" href="/hub/logout">Sign out</a>', page)
        self.assertNotIn("still running", page)

    def test_warns_that_a_running_server_keeps_using_credit(self):
        page = self.render(user=types.SimpleNamespace(name="tendai", active=True))
        self.assertIn("Your server is still running", page)

    def test_nothing_to_ask_when_signed_out(self):
        page = self.render(user=None)
        self.assertNotIn("z-logout", page.replace(".z-logout", ""))
        self.assertIn('id="login"', page)


if __name__ == "__main__":
    unittest.main()
