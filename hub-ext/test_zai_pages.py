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
        zai_platform.CATALOG.signup_credit = 0   # balances below start from zero
        zai_platform.init_db()

    def credit(self, micros, user="tendai"):
        zai_platform.add_credit(user, micros, "topup", "", "admin")

    def render(self, name, **variables):
        env = Environment(autoescape=True, loader=DictLoader({
            "page.html": "{% block main %}{% endblock %}{% block script %}{% endblock %}",
            name: open(os.path.join(REPO, "templates", name)).read()}))
        cat = zai_platform.CATALOG
        variables.setdefault("access", {"mine": None, "pending": [], "recent": []})
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
        self.assertIn("<b>$1.25</b><span>/ hour incl. VAT</span>", html)
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
        self.assertIn('data-rate="1250000"', html)
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

    def test_new_accounts_get_the_welcome_credit_once(self):
        zai_platform.CATALOG = zai_platform.Catalog.load()      # the shipped catalog: $5
        page = self.account()
        self.assertIn("$5.00", page)
        self.assertIn("Welcome credit", page)
        with zai_platform._db() as con:
            zai_platform.account(con, "tendai")
            self.assertEqual(con.execute("SELECT COUNT(*) FROM ledger WHERE kind='signup'").fetchone()[0], 1)

    def test_balance_in_hours_of_the_default_size(self):
        self.credit(20 * MICROS)
        page = self.account()
        self.assertIn("$20.00", page)
        self.assertIn("About <strong>16 hours</strong> on GPU · 18 GB", page)       # $20 at $1.25

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
        self.assertIn("<b>$1.25</b><span>/ hour incl. VAT</span>", page)
        self.assertIn("covers about <strong>16 hours</strong>", page)
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


class SelfServiceEndpoints(Case):
    """Members of serverless.creators_group create and delete their own endpoints."""

    def endpoint(self, owner):
        return {"id": "abcdefabcdef", "name": "echo", "owner": owner, "size": "cpu", "template": "pytorch",
                "workdir": "endpoints/echo", "command": "python handler.py", "env": [], "maxWorkers": 1,
                "idleTimeout": 30, "executionTimeout": 600, "jobs": [],
                "health": {"jobs": {"inQueue": 0, "inProgress": 0}, "lastError": None,
                           "workers": {"idle": 0, "running": 0, "initializing": 0}}}

    def page(self, user, endpoints=()):
        return self.render("endpoints.html", user=user, endpoints=list(endpoints), unavailable="", spend={},
                           message="", error="", public_host="h", users=["alice", "tendai"], now_ms=0, form={},
                           can_create=zai_platform.can_create_endpoints(user))

    def member(self, name="tendai", groups=("endpoint-creators",)):
        u = person(name)
        u.groups = [types.SimpleNamespace(name=g) for g in groups]
        return u

    def test_who_may_create(self):
        self.assertEqual(zai_platform.CATALOG.sv_creators_group, "endpoint-creators")
        self.assertTrue(zai_platform.can_create_endpoints(self.member()))
        self.assertFalse(zai_platform.can_create_endpoints(self.member(groups=("other",))))
        admin = self.member(groups=())
        admin.admin = True
        self.assertTrue(zai_platform.can_create_endpoints(admin))

    def test_no_group_configured_means_admins_only(self):
        zai_platform.CATALOG.sv_creators_group = ""
        self.assertFalse(zai_platform.can_create_endpoints(self.member()))

    def test_member_gets_the_form_without_an_owner_choice(self):
        page = self.page(self.member())
        self.assertIn('id="create"', page)
        self.assertNotIn('name="owner"', page)
        self.assertIn("create the endpoint below", page)
        self.assertIn("Folder in your home", page)

    def test_non_member_is_told_to_ask(self):
        page = self.page(self.member(groups=()))
        self.assertNotIn('id="create"', page)
        self.assertIn("ask for access below", page)
        self.assertIn('value="request"', page)
        zai_platform.CATALOG.sv_creators_group = ""        # admin-only hub: nothing to ask for
        page = self.page(self.member(groups=()))
        self.assertIn("ask an administrator to create the endpoint", page)
        self.assertNotIn('value="request"', page)

    def test_admin_still_picks_the_owner(self):
        admin = self.member(groups=())
        admin.admin = True
        page = self.page(admin)
        self.assertIn('name="owner"', page)
        self.assertIn("Folder in their home", page)

    def test_delete_shows_with_no_recent_jobs(self):
        page = self.page(self.member(), [self.endpoint("tendai")])
        self.assertIn("No jobs in the last 30 minutes", page)
        self.assertGreater(page.index('value="delete"'), page.rindex("</details>"))   # not folded away

    def test_member_deletes_only_their_own(self):
        self.assertIn('value="delete"', self.page(self.member(), [self.endpoint("tendai")]))
        self.assertNotIn('value="delete"', self.page(self.member(groups=()), [self.endpoint("tendai")]))


class FlashHandler:
    """Enough of a BaseHandler for the POST side of Endpoints and Billing: body
    arguments in, signed cookies and a redirect or a render out."""
    SECRET = "s" * 32

    def __init__(self, user, body=None, cookies=None):
        self.current_user, self.body, self.cookies = user, body or {}, dict(cookies or {})
        self.hub = types.SimpleNamespace(base_url="/hub/")
        self.log = types.SimpleNamespace(info=lambda *a: None)
        self.db = types.SimpleNamespace(query=lambda m: types.SimpleNamespace(
            filter_by=lambda **k: types.SimpleNamespace(first=lambda: object())))
        self.redirected, self.rendered = None, None

    def get_body_argument(self, name, default=None):
        return self.body.get(name, default)

    def _set_cookie(self, key, value, **kw):
        from tornado.web import create_signed_value
        self.cookies[key] = create_signed_value(self.SECRET, key, value).decode()

    def get_cookie(self, key):
        return self.cookies.get(key)

    def get_secure_cookie(self, key, max_age_days=31):
        from tornado.web import decode_signed_value
        return decode_signed_value(self.SECRET, key, self.cookies.get(key), max_age_days=max_age_days)

    def clear_cookie(self, key, **kw):
        self.cookies.pop(key, None)

    def redirect(self, url):
        self.redirected = url

    async def get(self, message="", error=""):
        self.rendered = {"message": message or zai_platform.take_flash(self), "error": error}


class NoRepeatOnReload(Case):
    """After a POST that changed something the browser is sent to a GET, so
    reloading the page doesn't queue the job or add the top-up again."""

    def setUp(self):
        super().setUp()
        self.sent = []

        async def gateway(user, method, path, body=None):
            self.sent.append((method, path))
            return {"id": "4b021fb2-0000-0000-0000-000000000000"}
        self._gateway, zai_platform.gateway = zai_platform.gateway, gateway

    def tearDown(self):
        zai_platform.gateway = self._gateway

    def post(self, cls, user, body, cookies=None):
        import asyncio
        h = FlashHandler(user, body, cookies)
        h._admin = lambda: None
        asyncio.run(cls.post(h))
        return h

    def test_send_job_redirects_and_the_message_shows_once(self):
        h = self.post(zai_platform.EndpointsPage, person(),
                      {"action": "run", "endpoint": "yops5sh2btvu", "input": '{"hi": 1}'})
        self.assertEqual(h.redirected, "/hub/endpoints")
        self.assertIsNone(h.rendered)
        self.assertEqual(self.sent, [("POST", "/v2/yops5sh2btvu/run")])
        self.assertEqual(zai_platform.take_flash(h), "Job 4b021fb2 queued. It appears under Recent jobs.")
        self.assertEqual(zai_platform.take_flash(h), "")       # a second reload: no message, no job
        self.assertEqual(len(self.sent), 1)

    def test_a_failed_action_renders_in_place(self):
        h = self.post(zai_platform.EndpointsPage, person(),
                      {"action": "run", "endpoint": "yops5sh2btvu", "input": "{hi"})
        self.assertIsNone(h.redirected)
        self.assertIn("not valid JSON", h.rendered["error"])
        self.assertEqual(self.sent, [])

    def test_top_up_redirects_and_a_reload_adds_nothing(self):
        h = self.post(zai_platform.BillingHandler, person("boss", admin=True),
                      {"username": "tendai", "kind": "topup", "amount": "5", "note": "uat"})
        self.assertEqual(h.redirected, "/hub/billing")
        self.assertIn("$5.00 added to tendai", zai_platform.take_flash(h))
        with zai_platform._db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM ledger WHERE kind='topup'").fetchone()[0], 1)

    def test_an_old_or_forged_message_is_ignored(self):
        from unittest import mock
        h = FlashHandler(person())
        zai_platform.flash_redirect(h, "endpoints", "hello")
        with mock.patch("tornado.web.time.time", return_value=time.time() + 600):
            self.assertEqual(zai_platform.take_flash(h), "")
        self.assertEqual(zai_platform.take_flash(FlashHandler(person(), cookies={"zai-flash": "hello"})), "")


class AccessRequests(Case):
    """A user asks to create endpoints; an admin approves (they join
    creators_group in the hub database) or declines with a reason."""

    def setUp(self):
        super().setUp()
        from jupyterhub import orm
        self.hdb = orm.new_session_factory("sqlite:///:memory:")()
        for name, admin in (("tendai", False), ("boss", True)):
            self.hdb.add(orm.User(name=name, admin=admin))
        self.hdb.commit()
        self.sent = []
        self._notify = zai_platform.eq_notify
        zai_platform.eq_notify = lambda db, kind, req: self.sent.append((kind, req["username"], req.get("reason", "")))

    def tearDown(self):
        zai_platform.eq_notify = self._notify
        self.hdb.close()

    def user(self, name):
        from jupyterhub import orm
        return orm.User.find(self.hdb, name)

    def post(self, name, body):
        import asyncio
        h = FlashHandler(self.user(name), body)
        h.db = self.hdb
        asyncio.run(zai_platform.EndpointsPage.post(h))
        return h

    def rows(self):
        with zai_platform._db() as con:
            return [dict(r) for r in con.execute("SELECT * FROM zai_endpoint_requests ORDER BY id")]

    def decide(self, rid, decision, reason="", by="boss"):
        return self.post(by, {"action": "request-decide", "id": str(rid), "decision": decision, "reason": reason})

    def page(self, name):
        u = self.user(name)
        return self.render("endpoints.html", user=u, endpoints=[], unavailable="", spend={}, message="",
                           error="", public_host="h", users=[], now_ms=0, form={},
                           can_create=zai_platform.can_create_endpoints(u),
                           access=zai_platform.eq_view(u.name, u.admin), note_max=500, reason_max=300)

    def test_ask_once_and_admins_hear(self):
        h = self.post("tendai", {"action": "request", "note": "  speech to text  "})
        self.assertEqual(h.redirected, "/hub/endpoints")
        self.assertEqual([(r["username"], r["status"], r["note"]) for r in self.rows()],
                         [("tendai", "pending", "speech to text")])
        self.assertEqual(self.sent, [("asked", "tendai", "")])
        again = self.post("tendai", {"action": "request", "note": ""})
        self.assertIn("already have a request waiting", again.rendered["error"])
        self.assertEqual(len(self.rows()), 1)

    def test_no_request_when_already_allowed_or_admin_only(self):
        self.assertIn("already create", self.post("boss", {"action": "request"}).rendered["error"])
        zai_platform.CATALOG.sv_creators_group = ""
        self.assertIn("Only administrators", self.post("tendai", {"action": "request"}).rendered["error"])
        self.assertEqual(self.rows(), [])

    def test_note_too_long(self):
        self.assertIn("500 characters", self.post("tendai", {"action": "request", "note": "x" * 501}).rendered["error"])

    def test_cancel_then_ask_again(self):
        self.post("tendai", {"action": "request"})
        self.post("tendai", {"action": "request-cancel"})
        self.post("tendai", {"action": "request"})
        self.assertEqual([r["status"] for r in self.rows()], ["cancelled", "pending"])

    def test_approve_adds_them_to_the_group_once(self):
        self.post("tendai", {"action": "request"})
        self.assertFalse(zai_platform.can_create_endpoints(self.user("tendai")))
        h = self.decide(self.rows()[0]["id"], "approve")
        self.assertEqual(h.redirected, "/hub/endpoints")
        self.assertTrue(zai_platform.can_create_endpoints(self.user("tendai")))
        self.assertEqual(self.rows()[0]["status"], "approved")
        self.assertEqual(self.rows()[0]["decided_by"], "boss")
        self.assertIn(("approved", "tendai", ""), self.sent)
        again = self.decide(self.rows()[0]["id"], "approve")
        self.assertIn("already approved", again.rendered["error"])

    def test_a_failed_group_change_leaves_the_request_waiting(self):
        self.post("tendai", {"action": "request"})
        rid = self.rows()[0]["id"]
        self.hdb.delete(self.user("tendai"))
        self.hdb.commit()
        self.assertIn("no user 'tendai'", self.decide(rid, "approve").rendered["error"])
        self.assertEqual(self.rows()[0]["status"], "pending")

    def test_decline_needs_a_reason_the_user_sees(self):
        self.post("tendai", {"action": "request"})
        rid = self.rows()[0]["id"]
        self.assertIn("Say why", self.decide(rid, "decline", "  ").rendered["error"])
        self.decide(rid, "decline", "Use the shared demo endpoint for now")
        self.assertEqual(self.rows()[0]["status"], "declined")
        self.assertIn(("declined", "tendai", "Use the shared demo endpoint for now"), self.sent)
        self.assertFalse(zai_platform.can_create_endpoints(self.user("tendai")))
        page = self.page("tendai")
        self.assertIn("was declined", page)
        self.assertIn("Use the shared demo endpoint for now", page)
        self.assertIn('value="request"', page)          # may ask again

    def test_only_admins_decide(self):
        from tornado.web import HTTPError
        self.post("tendai", {"action": "request"})
        with self.assertRaises(HTTPError) as cm:
            self.decide(self.rows()[0]["id"], "approve", by="tendai")
        self.assertEqual(cm.exception.status_code, 403)

    def test_pages(self):
        page = self.page("tendai")
        self.assertIn("Want to create your own endpoints?", page)
        self.assertIn("ask for access below", page)
        self.assertNotIn('id="create"', page)
        self.post("tendai", {"action": "request", "note": "whisper <b>api</b>"})
        page = self.page("tendai")
        self.assertIn("Your request is waiting", page)
        self.assertIn('value="request-cancel"', page)
        admin = self.page("boss")
        self.assertIn('id="requests"', admin)
        self.assertIn("whisper &lt;b&gt;api&lt;/b&gt;", admin)
        self.assertIn('value="approve"', admin)
        self.assertIn('href="/hub/authorize#tendai"', admin)
        self.assertEqual(zai_platform.eq_pending_count(self.user("boss")), 1)
        self.assertEqual(zai_platform.eq_pending_count(self.user("tendai")), 0)
        self.decide(self.rows()[0]["id"], "approve")
        self.assertIn('id="create"', self.page("tendai"))
        self.assertIn("Recently decided", self.page("boss"))
        self.assertEqual(zai_platform.eq_pending_count(self.user("boss")), 0)

    def test_emails_go_through_the_reset_mailer_when_it_is_on(self):
        import asyncio
        from nativeauthenticator.orm import UserInfo
        UserInfo.__table__.create(self.hdb.get_bind(), checkfirst=True)
        for name in ("tendai", "boss"):
            self.hdb.add(UserInfo(username=name, email=f"{name}@example.org", password=b"x"))
        self.hdb.commit()
        mail = []

        async def send(to, subject, text):
            mail.append((to, subject, text))
        rs = types.SimpleNamespace(_RS={"on": False, "public": "https://hub.example"}, _rs_send=send)
        saved, sys.modules["zai_reset"] = sys.modules.get("zai_reset"), rs
        try:
            async def run(kind, req):
                self._notify(self.hdb, kind, req)
                await asyncio.sleep(0)
            asyncio.run(run("asked", {"username": "tendai", "note": "whisper"}))
            self.assertEqual(mail, [])                       # mail off: nothing, no error
            rs._RS["on"] = True
            asyncio.run(run("asked", {"username": "tendai", "note": "whisper"}))
            asyncio.run(run("declined", {"username": "tendai", "reason": "not yet"}))
        finally:
            if saved is None:
                sys.modules.pop("zai_reset", None)
            else:
                sys.modules["zai_reset"] = saved
        self.assertEqual([m[0] for m in mail], ["boss@example.org", "tendai@example.org"])
        self.assertIn("https://hub.example/hub/endpoints#requests", mail[0][2])
        self.assertIn("whisper", mail[0][2])
        self.assertIn("not yet", mail[1][2])

    def test_nav_count(self):
        env = Environment(autoescape=True, loader=DictLoader({
            "templates/page.html": "{% block nav_bar_left_items %}{% endblock %}{% block login_widget %}{% endblock %}",
            "page.html": open(os.path.join(REPO, "templates", "page.html")).read()}))
        boss = types.SimpleNamespace(name="boss", admin=True, active=False)
        nav = env.get_template("page.html").render(base_url="/hub/", user=boss, zai_serverless=True,
                                                    zai_endpoint_requests=2, logout_url="", login_url="")
        self.assertIn('href="/hub/endpoints#requests">Endpoints <span class="z-nav-balance">(2)</span>', nav)


if __name__ == "__main__":
    unittest.main()
