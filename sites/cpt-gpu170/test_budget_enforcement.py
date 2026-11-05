"""Tests for GPU budget enforcement in values.yaml (08-budget-enforcement).

The code under test is the text of the config blocks, executed here in one
namespace as the chart executes them (08 uses helpers from 03 and 04). The
usage meter is replaced by a function the tests control. It needs the hub's
packages:

    pip install jupyterhub jupyterhub-nativeauthenticator==1.2.0 pyyaml
    cd sites/cpt-gpu170 && python3 -m unittest -v test_budget_enforcement
"""
import json
import os
import types
import unittest

import yaml
from jinja2 import DictLoader, Environment
from jupyterhub import orm
from nativeauthenticator import NativeAuthenticator
from nativeauthenticator.orm import UserInfo
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tornado import web
from traitlets.config import Config

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
VALUES = yaml.safe_load(open(os.path.join(HERE, "values.yaml")))
BLOCKS = VALUES["hub"]["extraConfig"]
SETTINGS = ("BUDGET_ENFORCE", "BUDGET_GRACE_MINUTES", "BUDGET_WARN_AT", "BUDGET_CHECK_SECONDS")
_GET_HANDLERS = NativeAuthenticator.get_handlers
T0 = 1_800_000_000.0          # an arbitrary "now", in seconds
MINUTE = 60.0


class FakeSpawner:
    def __init__(self, profile="gpu-standard", ready=True, pending=None):
        self.user_options = {"profile": profile}
        self.ready, self.pending = ready, pending
        self._stop_pending = False


class FakeUser:
    def __init__(self, name, log, **spawner):
        self.name, self.log = name, log
        self.spawner = FakeSpawner(**spawner)

    async def stop(self, server_name):
        self.log.append(("stop", self.name))
        self.spawner.ready = False


class FakeProxy:
    def __init__(self, log):
        self.log = log

    async def delete_user(self, user, server_name):
        self.log.append(("unroute", user.name))


class Meter:
    """Stands in for _meter(): answers /api/check and records /api/budget."""

    def __init__(self):
        self.usage = {}           # user -> (hours used, budget)
        self.month = "2026-10"
        self.down = False
        self.calls, self.budgets = [], []

    async def __call__(self, path, body=None, raw=False, timeout=10):
        self.calls.append(path)
        if self.down:
            raise RuntimeError("usage meter unreachable")
        if path == "/api/budget":
            self.budgets.append(body)
            return {"ok": True}
        user = path.rsplit("/", 1)[1]
        used, budget = self.usage.get(user, (0, None))
        return {"month": self.month, "gpu_hours_used": used, "budget": budget,
                "allowed": budget is None or used < budget}


class Case(unittest.IsolatedAsyncioTestCase):
    enforce = "true"

    async def asyncSetUp(self):
        NativeAuthenticator.get_handlers = _GET_HANDLERS
        self._old_env = {k: os.environ.get(k) for k in SETTINGS}
        os.environ.update(BUDGET_ENFORCE=self.enforce, BUDGET_GRACE_MINUTES="15")
        for key in ("BUDGET_WARN_AT", "BUDGET_CHECK_SECONDS"):
            os.environ.pop(key, None)

        engine = create_engine("sqlite://")
        orm.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.config = Config()
        self.config.JupyterHub.template_vars = {}
        self.config.KubeSpawner.profile_list = VALUES["singleuser"]["profileList"]
        self.ns = {"c": self.config}
        for name in ("03-gpu-capacity", "04-usage", "08-budget-enforcement"):
            exec(BLOCKS[name], self.ns)
        self.meter = self.ns["_meter"] = Meter()

        self.log, self.users = [], {}
        self.app = types.SimpleNamespace(db=self.db, users=self.users, proxy=FakeProxy(self.log))
        self.ns["_bw_table"].create(self.db.get_bind(), checkfirst=True)
        self.ns["_bw"]["db"] = self.db
        self.page_vars = {k: (v(None) if callable(v) else v)
                          for k, v in self.config.JupyterHub.template_vars.items()}

    async def asyncTearDown(self):
        NativeAuthenticator.get_handlers = _GET_HANDLERS
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def user(self, name, used, budget, **spawner):
        """A person with a running server, known to the hub's database."""
        row = orm.User(name=name)
        self.db.add(row)
        self.db.add(orm.Spawner(user=row, name="", server=orm.Server()))
        self.db.commit()
        self.users[row] = FakeUser(name, self.log, **spawner)
        self.meter.usage[name] = (used, budget)
        return self.users[row]

    async def check(self, user, at=T0):
        return await self.ns["_bw_check"](self.app, user, now=at)

    def state(self, name, month="2026-10"):
        return self.ns["_bw_load"](self.db, name, month)


class Decisions(Case):
    """_bw_step: what should happen, given usage and what was already done."""

    def step(self, used, budget, at=T0, **state):
        return self.ns["_bw_step"](dict(self.ns["_BW_NOTHING"], **state), used, budget, at)

    def test_under_ninety_percent_is_left_alone(self):
        self.assertEqual(self.step(26.9, 30)[1], None)

    def test_ninety_percent_warns_once(self):
        state, action = self.step(27, 30)
        self.assertEqual((action, state["warned_at"]), ("warn", T0))
        self.assertEqual(self.step(28, 30, at=T0 + 300, **state)[1], None)

    def test_running_out_starts_the_grace_period(self):
        state, action = self.step(30, 30)
        self.assertEqual((action, state["final_at"], state["warned_at"]), ("final", T0, T0))

    def test_no_stop_before_the_grace_period_ends(self):
        state, _ = self.step(30, 30)
        self.assertEqual(self.step(30.2, 30, at=T0 + 14 * MINUTE, **state)[1], None)

    def test_stop_when_the_grace_period_ends(self):
        state, _ = self.step(30, 30)
        self.assertEqual(self.step(30.3, 30, at=T0 + 15 * MINUTE, **state)[1], "stop")

    def test_raising_the_budget_during_grace_cancels_the_stop(self):
        state, _ = self.step(30, 30)
        state, action = self.step(30.1, 40, at=T0 + 10 * MINUTE, **state)
        self.assertEqual((action, state["final_at"], state["warned_at"]), ("clear", None, None))
        self.assertEqual(self.step(30.4, 40, at=T0 + 60 * MINUTE, **state)[1], None)

    def test_a_small_top_up_still_leaves_them_warned(self):
        state, _ = self.step(30, 30)
        state, action = self.step(30.1, 32, at=T0 + 5 * MINUTE, **state)
        self.assertEqual((action, state["final_at"]), ("clear", None))
        self.assertIsNotNone(state["warned_at"])

    def test_no_budget_means_no_action_ever(self):
        self.assertEqual(self.step(5000, None), (self.ns["_BW_NOTHING"], None))

    def test_a_zero_budget_counts_as_used_up(self):
        self.assertEqual(self.step(0, 0)[1], "final")

    def test_a_stop_already_made_is_not_repeated(self):
        state = {"warned_at": T0, "final_at": T0, "stopped_at": T0 + 15 * MINUTE, "enforced": False}
        self.assertEqual(self.step(31, 30, at=T0 + 60 * MINUTE, **state)[1], None)


class Enforcing(Case):
    async def test_the_whole_course_of_one_month(self):
        alice = self.user("alice", 20, 30)
        self.assertIsNone(await self.check(alice))
        self.meter.usage["alice"] = (27.5, 30)
        self.assertEqual(await self.check(alice, T0 + 1 * MINUTE), "warn")
        self.assertIsNone(await self.check(alice, T0 + 3 * MINUTE))
        self.meter.usage["alice"] = (30, 30)
        self.assertEqual(await self.check(alice, T0 + 5 * MINUTE), "final")
        self.assertIsNone(await self.check(alice, T0 + 19 * MINUTE))
        self.assertEqual(self.log, [])
        self.assertEqual(await self.check(alice, T0 + 20 * MINUTE), "stop")
        # Off the proxy first, then stopped: the order the hub's own Stop uses.
        self.assertEqual(self.log, [("unroute", "alice"), ("stop", "alice")])
        state = self.state("alice")
        self.assertEqual((state["stopped_at"], state["enforced"], state["final_at"]),
                         (T0 + 20 * MINUTE, True, None))

    async def test_a_cpu_server_is_never_looked_at(self):
        bob = self.user("bob", 500, 30, profile="cpu")
        self.assertIsNone(await self.check(bob))
        self.assertEqual((self.meter.calls, self.log), ([], []))

    async def test_a_server_that_is_not_running_is_never_looked_at(self):
        bob = self.user("bob", 500, 30, ready=False)
        self.assertIsNone(await self.check(bob))
        self.assertEqual(self.meter.calls, [])

    async def test_someone_with_no_budget_is_never_stopped(self):
        carol = self.user("carol", 5000, None)
        for minutes in (0, 20, 600):
            self.assertIsNone(await self.check(carol, T0 + minutes * MINUTE))
        self.assertEqual(self.log, [])
        self.assertEqual(self.state("carol"), self.ns["_BW_NOTHING"])

    async def test_an_unreachable_meter_stops_nothing(self):
        dave = self.user("dave", 30, 30)
        await self.check(dave)                            # final warning given
        self.meter.down = True
        with self.assertLogs(level="WARNING") as logs:
            await self.ns["_bw_pass"](self.app)
        self.assertIn("could not check dave, doing nothing", "\n".join(logs.output))
        self.assertEqual(self.log, [])

    async def test_one_persons_failure_does_not_skip_the_others(self):
        self.user("dave", 30, 30)
        erin = self.user("erin", 28, 30)
        original = self.ns["_bw_check"]

        async def flaky(app, user, now=None):
            if user.name == "dave":
                raise RuntimeError("boom")
            return await original(app, user, now=now)
        self.ns["_bw_check"] = flaky
        with self.assertLogs(level="WARNING"):
            await self.ns["_bw_pass"](self.app)
        self.assertIsNotNone(self.state(erin.name)["warned_at"])

    async def test_a_server_already_stopping_is_left_for_the_next_pass(self):
        frank = self.user("frank", 30, 30)
        await self.check(frank)
        frank.spawner.pending = "stop"
        self.assertIsNone(await self.check(frank, T0 + 20 * MINUTE))
        self.assertEqual(self.log, [])
        self.assertIsNone(self.state("frank")["stopped_at"])

    async def test_what_was_decided_survives_a_hub_restart(self):
        gina = self.user("gina", 30, 30)
        await self.check(gina)
        # A restarted hub runs the config again, on the same database.
        config = Config()
        config.JupyterHub.template_vars = {}
        config.KubeSpawner.profile_list = VALUES["singleuser"]["profileList"]
        fresh = {"c": config}
        for name in ("03-gpu-capacity", "04-usage", "08-budget-enforcement"):
            exec(BLOCKS[name], fresh)
        fresh["_meter"] = self.meter
        self.assertEqual(await fresh["_bw_check"](self.app, gina, now=T0 + 20 * MINUTE), "stop")

    async def test_a_new_month_starts_clean(self):
        hank = self.user("hank", 30, 30)
        await self.check(hank)
        self.meter.month, self.meter.usage["hank"] = "2026-11", (0.2, 30)
        self.assertIsNone(await self.check(hank, T0 + 20 * MINUTE))
        self.assertEqual(self.log, [])
        self.assertEqual(self.state("hank", "2026-11"), self.ns["_BW_NOTHING"])

    async def test_running_over_budget_again_gets_a_fresh_grace_period(self):
        ivy = self.user("ivy", 30, 30)
        await self.check(ivy)
        await self.check(ivy, T0 + 15 * MINUTE)           # stopped
        ivy.spawner.ready = True                          # running again, still over
        self.log.clear()
        self.assertEqual(await self.check(ivy, T0 + 60 * MINUTE), "final")
        self.assertIsNone(await self.check(ivy, T0 + 70 * MINUTE))
        self.assertEqual(await self.check(ivy, T0 + 75 * MINUTE), "stop")


class WarnOnly(Case):
    enforce = "false"

    async def test_nothing_is_stopped_and_the_log_says_what_would_have_been(self):
        alice = self.user("alice", 30, 30)
        self.assertEqual(await self.check(alice), "final")
        with self.assertLogs(level="WARNING") as logs:
            self.assertEqual(await self.check(alice, T0 + 15 * MINUTE), "would-stop")
        self.assertIn("gpu budget: would-stop alice (30.0 of 30 GPU hours in 2026-10)",
                      "\n".join(logs.output))
        self.assertEqual(self.log, [])
        self.assertTrue(alice.spawner.ready)
        state = self.state("alice")
        self.assertEqual((state["stopped_at"], state["enforced"]), (T0 + 15 * MINUTE, False))

    async def test_it_is_said_once_not_every_pass(self):
        alice = self.user("alice", 30, 30)
        await self.check(alice)
        await self.check(alice, T0 + 15 * MINUTE)
        for minutes in (17, 30, 600):
            self.assertIsNone(await self.check(alice, T0 + minutes * MINUTE))

    def test_warn_only_is_the_shipped_default(self):
        self.assertEqual(VALUES["hub"]["extraEnv"]["BUDGET_ENFORCE"], "false")
        self.assertEqual(VALUES["hub"]["extraEnv"]["BUDGET_GRACE_MINUTES"], "15")
        self.assertFalse(self.page_vars["budget_enforce"])


class Handler:
    """Just enough of a JupyterHub handler to call the two new ones on."""

    def __init__(self, case, name, admin=False, body=None, user=None):
        self.current_user = types.SimpleNamespace(
            name=name, admin=admin, spawner=(user.spawner if user else FakeSpawner(ready=False)))
        self.db = case.db
        self.hub = types.SimpleNamespace(base_url="/hub/")
        self.authenticator = types.SimpleNamespace(ask_email_on_signup=True)
        self.body = body or {}
        self.headers, self.out, self.redirected_to, self.rendered = {}, None, None, None

    def get_body_argument(self, name, default=""):
        return self.body.get(name, default)

    def set_header(self, key, value):
        self.headers[key] = value

    def finish(self, out=None):
        self.out = out

    def redirect(self, url):
        self.redirected_to = url

    async def render_template(self, name, **ns):
        self.rendered = (name, ns)
        return "page"


class Notice(Case):
    async def notice(self, name, user=None):
        handler = Handler(self, name, user=user)
        await self.ns["BudgetNotice"].get(handler)
        self.assertEqual(handler.headers["Cache-Control"], "no-store")
        return json.loads(handler.out)

    async def test_stages(self):
        alice = self.user("alice", 10, 30)
        self.assertEqual((await self.notice("alice", alice))["stage"], "none")
        self.meter.usage["alice"] = (27, 30)
        seen = await self.notice("alice", alice)
        self.assertEqual((seen["stage"], seen["percent"], seen["enforce"]), ("warn", 90, True))
        self.meter.usage["alice"] = (30, 30)
        self.assertEqual((await self.notice("alice", alice))["stage"], "over")
        await self.check(alice)                           # the watcher's final warning
        seen = await self.notice("alice", alice)
        self.assertEqual(seen["stage"], "final")
        self.assertTrue(seen["stops_at"].endswith("+00:00"))

    async def test_no_countdown_without_a_running_gpu_server(self):
        alice = self.user("alice", 30, 30)
        await self.check(alice)
        alice.spawner.ready = False
        self.assertEqual((await self.notice("alice", alice))["stage"], "over")

    async def test_no_budget_shows_nothing(self):
        self.user("carol", 5000, None)
        self.assertEqual(await self.notice("carol"), {"stage": "none"})

    async def test_meter_down_shows_nothing_rather_than_an_error(self):
        self.user("alice", 30, 30)
        self.meter.down = True
        self.assertEqual(await self.notice("alice"), {"stage": "none"})


class NoticeWarnOnly(Case):
    enforce = "false"

    async def test_warn_only_never_promises_a_stop(self):
        alice = self.user("alice", 30, 30)
        await self.check(alice)
        handler = Handler(self, "alice", user=alice)
        await self.ns["BudgetNotice"].get(handler)
        seen = json.loads(handler.out)
        self.assertEqual((seen["stage"], seen["enforce"]), ("over", False))
        self.assertNotIn("stops_at", seen)


class ApproveWithHours(Case):
    def applicant(self, name="tendai", approved=False):
        self.db.add(UserInfo(username=name, password=b"x", email=name + "@zai.example",
                             is_authorized=approved))
        self.db.commit()

    async def approve(self, name, hours, admin=True):
        handler = Handler(self, "boss", admin=admin, body={"gpu_hours": hours})
        await self.ns["ApproveWithBudget"].post(handler, name)
        return handler

    async def test_sets_the_budget_then_approves(self):
        self.applicant()
        handler = await self.approve("tendai", "30")
        self.assertEqual(self.meter.budgets, [
            {"user": "tendai", "gpu_hours": 30.0, "by": "boss", "note": "Set at approval"}])
        self.assertTrue(UserInfo.find(self.db, "tendai").is_authorized)
        self.assertEqual(handler.redirected_to, "/hub/authorize#tendai")

    async def test_empty_hours_approves_without_touching_budgets(self):
        self.applicant()
        await self.approve("tendai", "")
        self.assertEqual(self.meter.budgets, [])
        self.assertTrue(UserInfo.find(self.db, "tendai").is_authorized)

    async def test_meter_failure_means_not_approved(self):
        self.applicant()
        self.meter.down = True
        handler = await self.approve("tendai", "30")
        self.assertFalse(UserInfo.find(self.db, "tendai").is_authorized)
        name, ns = handler.rendered
        self.assertEqual(name, "authorization-area.html")
        self.assertIn("tendai was NOT approved", ns["flash"])

    async def test_bad_hours_are_refused(self):
        self.applicant()
        for hours in ("-5", "2.5", "lots", "1e3", "²"):
            with self.subTest(hours=hours):
                handler = await self.approve("tendai", hours)
                self.assertIn("whole number", handler.rendered[1]["flash"])
        self.assertEqual(self.meter.budgets, [])
        self.assertFalse(UserInfo.find(self.db, "tendai").is_authorized)

    async def test_only_admins(self):
        self.applicant()
        with self.assertRaises(web.HTTPError) as raised:
            await self.approve("tendai", "30", admin=False)
        self.assertEqual(raised.exception.status_code, 403)
        self.assertFalse(UserInfo.find(self.db, "tendai").is_authorized)

    async def test_unknown_or_malformed_user_is_a_404(self):
        for name in ("nobody", "../etc", "Tendai"):
            with self.subTest(name=name):
                with self.assertRaises(web.HTTPError) as raised:
                    await self.approve(name, "30")
                self.assertEqual(raised.exception.status_code, 404)

    async def test_someone_already_approved_is_not_un_approved(self):
        # The stock Approve link is a toggle; this must never be.
        self.applicant(approved=True)
        await self.approve("tendai", "40")
        self.assertTrue(UserInfo.find(self.db, "tendai").is_authorized)
        self.assertEqual(self.meter.budgets[0]["gpu_hours"], 40.0)


class Pages(Case):
    def render(self, name, **variables):
        env = Environment(autoescape=True, loader=DictLoader({
            "page.html": "{% block main %}{% endblock %}",
            name: open(os.path.join(REPO, "templates", name)).read()}))
        return env.get_template(name).render(base_url="/hub/", xsrf="token", **variables)

    def pending(self):
        self.db.add(UserInfo(username="tendai", password=b"x", email="t@zai.example"))
        self.db.commit()
        return self.db.query(UserInfo).all()

    def test_approve_form_is_filled_with_the_hours_asked_for(self):
        asked = lambda name: {"who": "Tendai", "type": "Individual", "gpu_hours": 30,
                              "submitted": "", "rows": []}
        page = self.render("authorization-area.html", ask_email=True, users=self.pending(),
                           access_request=asked, **self.page_vars)
        self.assertIn('action="/hub/authorize-budget/tendai"', page)
        self.assertIn('name="_xsrf" value="token"', page)
        self.assertRegex(page, r'name="gpu_hours"[^>]*\n?[^>]*value="30"')

    def test_approve_form_is_empty_without_an_application(self):
        page = self.render("authorization-area.html", ask_email=True, users=self.pending(),
                           **self.page_vars)
        self.assertRegex(page, r'name="gpu_hours"[^>]*\n?[^>]*value=""')

    def test_without_the_feature_approve_is_the_plain_link(self):
        page = self.render("authorization-area.html", ask_email=True, users=self.pending())
        self.assertIn('href="/hub/authorize/tendai">Approve</a>', page)
        self.assertNotIn("authorize-budget", page)

    def test_a_refusal_is_shown_on_the_approve_page(self):
        page = self.render("authorization-area.html", ask_email=True, users=self.pending(),
                           flash="tendai was NOT approved", **self.page_vars)
        self.assertIn('role="alert">tendai was NOT approved</p>', page)

    async def test_admin_markers(self):
        alice = self.user("alice", 30, 30)
        await self.check(alice)
        marks = self.page_vars["budget_marks"]
        self.assertEqual([label for label, _ in marks("alice", "2026-10")], ["final warning"])
        await self.check(alice, T0 + 15 * MINUTE)
        self.assertEqual([label for label, _ in marks("alice", "2026-10")], ["stopped"])
        self.assertEqual(marks("alice", "2026-09"), [])
        self.assertEqual(marks("nobody", "2026-10"), [])

    def test_pages_carry_the_notice(self):
        home = open(os.path.join(REPO, "templates", "home.html")).read()
        usage = open(os.path.join(REPO, "templates", "usage.html")).read()
        self.assertIn("usage/notice.json", home)
        self.assertIn('id="z-budget-notice"', home)
        self.assertIn("usage/notice.json", usage)
        self.assertIn("Warn-only mode", usage)
        # The 90% warning mentions a stop only when one will happen.
        self.assertIn("(d.enforce", home)

    def test_handlers_are_registered_and_the_watcher_starts_once(self):
        auth = types.SimpleNamespace(log=types.SimpleNamespace(error=self.fail))
        started = []
        self.ns["_bw_forever"] = lambda app: started.append(app) or self._idle()
        handlers = NativeAuthenticator.get_handlers(auth, self.app)
        NativeAuthenticator.get_handlers(auth, self.app)
        paths = [h[0] for h in handlers]
        self.assertIn(r"/usage/notice\.json", paths)
        self.assertIn(r"/authorize-budget/([^/]+)", paths)
        self.assertEqual(len(started), 1)
        self.ns["_bw"]["task"].cancel()

    async def _idle(self):
        pass


if __name__ == "__main__":
    unittest.main()
