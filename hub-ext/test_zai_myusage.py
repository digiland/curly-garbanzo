"""Tests for hub-ext/zai_myusage.py: a user's own usage page.

The module runs against the real zai_platform, its real catalog and a
throwaway database. It needs the hub's packages:

    pip install jupyterhub jupyterhub-kubespawner pyyaml
    cd hub-ext && python3 -m unittest -v test_zai_myusage
"""
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone

import yaml
from jinja2 import DictLoader, Environment

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
VALUES = yaml.safe_load(open(os.path.join(REPO, "charts", "jupyterhub-values.yaml")))
MICROS = 1_000_000
HOUR = 3600

# The real zai_platform on a stand-in for KubeSpawner, as in test_zai_credit.
# Other test files put their own stand-in zai_platform in sys.modules; it is
# put back after.
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

GPU = 1_320_000      # $1.32/h in micros
CPU = 60_000


def ts(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp()


class Case(unittest.TestCase):
    def setUp(self):
        db = zai_platform.DB_PATH
        if os.path.exists(db):
            os.remove(db)
        zai_platform.CATALOG = zai_platform.Catalog.load()
        zai_platform.init_db()

    def session(self, started, stopped, size="gpu-18", rate=GPU, user="tendai", kind="notebook"):
        gpu = size.startswith("gpu")
        with zai_platform._db() as con:
            con.execute(
                "INSERT INTO sessions(username, server, profile, gpu, started, stopped, kind, size, template,"
                " resource, gpus, rate, charged) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (user, "", size, int(gpu), started, stopped, kind, size, "pytorch",
                 "nvidia.com/gpu" if gpu else "", int(gpu), rate, int(stopped is not None)))

    def credit(self, when, micros, kind="topup", user="tendai"):
        with zai_platform._db() as con:
            con.execute("INSERT INTO ledger(username, micros, kind, note, actor, ts) VALUES (?,?,?,?,?,?)",
                        (user, micros, kind, "", "admin", when))

    def report(self, since, until, now=None, user="tendai"):
        with zai_platform._db() as con:
            return zai_myusage._mu_report(con, user, since, until, now or until)


class Window(unittest.TestCase):
    NOW = ts(2026, 10, 18, 12)

    def test_this_month(self):
        self.assertEqual(zai_myusage._mu_window("month", self.NOW), (ts(2026, 10, 1), self.NOW))

    def test_last_month(self):
        self.assertEqual(zai_myusage._mu_window("last-month", self.NOW), (ts(2026, 9, 1), ts(2026, 10, 1)))

    def test_last_month_in_january_is_december(self):
        self.assertEqual(zai_myusage._mu_window("last-month", ts(2027, 1, 5)), (ts(2026, 12, 1), ts(2027, 1, 1)))

    def test_thirty_days_and_all_time(self):
        self.assertEqual(zai_myusage._mu_window("30d", self.NOW), (self.NOW - 30 * 86400, self.NOW))
        self.assertEqual(zai_myusage._mu_window("all", self.NOW), (0.0, self.NOW))


class Report(Case):
    SINCE, UNTIL = ts(2026, 10, 1), ts(2026, 10, 18)

    def test_hours_and_cost_of_whole_sessions(self):
        self.session(ts(2026, 10, 5, 9), ts(2026, 10, 5, 11))                # 2 h GPU
        self.session(ts(2026, 10, 6, 9), ts(2026, 10, 6, 10), "cpu", CPU)   # 1 h CPU
        sessions, sizes, t = self.report(self.SINCE, self.UNTIL)
        self.assertEqual((t["gpu_secs"], t["cpu_secs"], t["sessions"]), (2 * HOUR, HOUR, 2))
        self.assertEqual(t["cost"], 2 * GPU + CPU)
        self.assertEqual([s["size"] for s in sessions], ["CPU only", "GPU · 18 GB"])   # newest first

    def test_session_crossing_the_start_counts_only_its_part_inside(self):
        self.session(ts(2026, 9, 30, 23), ts(2026, 10, 1, 1))     # 1 h before, 1 h inside
        _, _, t = self.report(self.SINCE, self.UNTIL)
        self.assertEqual((t["gpu_secs"], t["cost"]), (HOUR, GPU))
        _, _, last_month = self.report(ts(2026, 9, 1), self.SINCE)
        self.assertEqual(last_month["gpu_secs"], HOUR)

    def test_running_session_counts_up_to_now(self):
        self.session(ts(2026, 10, 17, 22), None)
        sessions, _, t = self.report(self.SINCE, self.UNTIL, now=ts(2026, 10, 17, 23))
        self.assertEqual((t["gpu_secs"], t["running"]), (HOUR, 1))
        self.assertTrue(sessions[0]["running"])

    def test_other_periods_and_other_people_are_left_out(self):
        self.session(ts(2026, 9, 2), ts(2026, 9, 3))
        self.session(ts(2026, 10, 5, 9), ts(2026, 10, 5, 11), user="rudo")
        sessions, sizes, t = self.report(self.SINCE, self.UNTIL)
        self.assertEqual((sessions, sizes, t["sessions"], t["cost"]), ([], [], 0, 0))

    def test_free_sessions_cost_nothing(self):
        self.session(ts(2026, 10, 5, 9), ts(2026, 10, 5, 11), rate=0)
        _, _, t = self.report(self.SINCE, self.UNTIL)
        self.assertEqual((t["gpu_secs"], t["cost"]), (2 * HOUR, 0))

    def test_by_size_most_hours_first_and_serverless_apart(self):
        self.session(ts(2026, 10, 5, 9), ts(2026, 10, 5, 10))
        self.session(ts(2026, 10, 6, 9), ts(2026, 10, 6, 12), "cpu", CPU)
        self.session(ts(2026, 10, 7, 9), ts(2026, 10, 7, 9, 30), kind="worker")
        _, sizes, _ = self.report(self.SINCE, self.UNTIL)
        self.assertEqual([(z["size"], z["sessions"]) for z in sizes],
                         [("CPU only", 1), ("GPU · 18 GB", 1), ("Serverless · GPU · 18 GB", 1)])

    def test_credit_added_in_the_period(self):
        self.credit(ts(2026, 10, 2), 25 * MICROS)
        self.credit(ts(2026, 10, 3), 5 * MICROS, kind="signup")
        self.credit(ts(2026, 10, 4), -3 * MICROS, kind="adjust")      # a correction, not credit added
        self.credit(ts(2026, 9, 20), 50 * MICROS)                     # before the period
        self.credit(ts(2026, 10, 2), 99 * MICROS, user="rudo")
        _, _, t = self.report(self.SINCE, self.UNTIL)
        self.assertEqual(t["credit"], 30 * MICROS)


class Month(Case):
    NOW = ts(2026, 10, 18, 12)

    def month(self):
        real = zai_myusage.time.time
        zai_myusage.time.time = lambda: self.NOW
        try:
            return zai_myusage._mu_month(types.SimpleNamespace(name="tendai", kind="user"))
        finally:
            zai_myusage.time.time = real

    def test_totals_and_fourteen_days_ending_today(self):
        self.session(ts(2026, 10, 18, 9), ts(2026, 10, 18, 11))      # today, 2 h
        self.session(ts(2026, 10, 10, 9), ts(2026, 10, 10, 10))      # 1 h
        self.session(ts(2026, 9, 30, 9), ts(2026, 9, 30, 10))        # last month: chart only
        m = self.month()
        self.assertEqual((m["cost"], m["gpu_secs"]), (3 * GPU, 3 * HOUR))
        self.assertEqual(len(m["days"]), 14)
        self.assertEqual((m["days"][0]["day"], m["days"][-1]["day"]), ("5 Oct", "18 Oct"))
        self.assertEqual([d["pct"] for d in m["days"] if d["pct"]], [50, 100])

    def test_free_account_charts_hours(self):
        self.session(ts(2026, 10, 18, 9), ts(2026, 10, 18, 10), rate=0)
        m = self.month()
        self.assertEqual((m["cost"], m["days"][-1]["pct"]), (0, 100))

    def test_nothing_yet(self):
        m = self.month()
        self.assertEqual((m["cost"], m["sessions"]), (0, 0))
        self.assertTrue(all(d["pct"] == 0 for d in m["days"]))

    def test_services_get_nothing(self):
        self.assertIsNone(zai_myusage._mu_month(None))


class Page(Case):
    def render(self, admin_free=False, **extra):
        with zai_platform._db() as con:
            sessions, sizes, totals = zai_myusage._mu_report(con, "tendai", 0, ts(2026, 10, 18), ts(2026, 10, 18))
        cat = zai_platform.CATALOG
        env = Environment(autoescape=True, loader=DictLoader({
            "page.html": "{% block main %}{% endblock %}",
            "my-usage.html": open(os.path.join(REPO, "templates", "my-usage.html")).read()}))
        return env.get_template("my-usage.html").render(
            base_url="/hub/", user=types.SimpleNamespace(name="tendai"), sessions=sessions, more=0,
            sizes=sizes, totals=totals, balance=7 * MICROS, period="month", periods=zai_myusage.PERIODS,
            catalog=cat, money=cat.money, rate_str=cat.rate_str, admin_free=admin_free,
            hours=lambda s: f"{s / 3600:,.1f}", when=zai_platform._when, dur=zai_platform._dur, **extra)

    def test_page_shows_totals_sessions_and_downloads(self):
        self.session(ts(2026, 10, 5, 9), ts(2026, 10, 5, 11))
        page = self.render()
        self.assertIn("$2.64", page)                       # 2 h at $1.32
        self.assertIn("$1.32/h incl. VAT", page)
        self.assertIn("2h 00m", page)
        self.assertIn("$7.00", page)                       # balance now
        self.assertIn("my-usage?period=month&amp;format=csv", page)
        self.assertIn("my-usage?period=month&amp;format=statement", page)
        for key in zai_myusage.PERIODS:
            self.assertIn(f"my-usage?period={key}", page)

    def test_free_admin_sees_hours_but_no_money(self):
        self.session(ts(2026, 10, 5, 9), ts(2026, 10, 5, 11), rate=0)
        page = self.render(admin_free=True)
        self.assertIn("Not charged", page)
        self.assertNotIn("Balance now", page)
        self.assertNotIn("<th class=\"num\">Cost</th>", page)

    def test_empty_period(self):
        self.assertIn("No servers ran in this period.", self.render())

    def test_nav_and_account_link_to_it(self):
        nav = open(os.path.join(REPO, "templates", "page.html")).read()
        self.assertIn('{% if user and not user.admin %}<li class="nav-item"><a class="nav-link" href="{{base_url}}my-usage">', nav)
        self.assertIn("my-usage", open(os.path.join(REPO, "templates", "account.html")).read())

    def test_the_hub_loads_the_module_after_the_platform_and_ships_it(self):
        startup = VALUES["hub"]["extraConfig"]["03-zai-platform"]
        self.assertLess(startup.index("zai_platform.configure(c)"), startup.index("zai_myusage.configure(c)"))
        deploy = open(os.path.join(REPO, "scripts", "05-jupyterhub.sh")).read()
        self.assertIn("--from-file=hub-ext/zai_myusage.py", deploy)
        self.assertIn("hub-ext/zai_myusage.py", deploy.split("ZAI_SUM=")[1].split("\n")[0])


if __name__ == "__main__":
    unittest.main()
