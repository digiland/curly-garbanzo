"""Tests for meter.py. Stdlib only:  python3 -m unittest -v test_meter"""
import datetime as dt
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import meter

UTC = dt.timezone.utc
# 2026-09-10 12:00:00 UTC, on a minute boundary
T0 = dt.datetime(2026, 9, 10, 12, 0, tzinfo=UTC).timestamp()
CFG = {"timezone": "UTC", "currency": "USD", "default_gpu_budget_hours": None,
       "idle_busy_threshold": 0.05, "resources": {},
       "rates": [{"from": "2026-01", "per_hour": {"gpu:mig-1g.35gb": 1.0, "cpu": 0.5}},
                 {"from": "2026-09", "per_hour": {"gpu:mig-1g.35gb": 2.0, "gpu:mig-2g.35gb": 4.0}}]}


def pod(uid, user, gpu=None, phase="Running", started=None, cpu="4", mem="32G",
        profile=None, component="singleuser-server"):
    # Default: started just before the test window, so nothing is backfilled
    # unless a test asks for it.
    started = T0 - 60 if started is None else started
    limits = {"cpu": cpu, "memory": mem}
    if gpu:
        limits[f"nvidia.com/{gpu}"] = "1"
    labels = {"component": component}
    if profile:
        labels["zai/profile"] = profile
    return {
        "metadata": {"uid": uid, "name": f"jupyter-{user}", "labels": labels,
                     "annotations": {"hub.jupyter.org/username": user}},
        "spec": {"containers": [{"name": "notebook", "resources": {"limits": limits}}]},
        "status": {"phase": phase,
                   "startTime": dt.datetime.fromtimestamp(started, UTC).isoformat().replace("+00:00", "Z")},
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = meter.connect(os.path.join(self.tmp.name, "m.sqlite"))
        meter.init_db(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def run_minutes(self, pods, start, minutes, dcgm=None):
        parsed = meter.parse_pods({"items": pods})
        for i in range(minutes):
            for off in (0, 20, 40):      # three passes per minute, like production
                meter.record(self.db, parsed, dcgm or {}, start + i * 60 + off)


class Parsing(Base):
    def test_cpu(self):
        self.assertEqual(meter.parse_cpu("4"), 4)
        self.assertEqual(meter.parse_cpu("500m"), 0.5)
        self.assertEqual(meter.parse_cpu(2), 2)
        self.assertEqual(meter.parse_cpu("junk"), 0)

    def test_mem(self):
        self.assertAlmostEqual(meter.parse_mem_gb("32G"), 32)
        self.assertAlmostEqual(meter.parse_mem_gb("32Gi"), 34.359738368)
        self.assertAlmostEqual(meter.parse_mem_gb("16000M"), 16)
        self.assertAlmostEqual(meter.parse_mem_gb("34359738368"), 34.359738368)
        self.assertEqual(meter.parse_mem_gb("12X"), 0)

    def test_pods_filtering(self):
        items = [pod("a", "alice", "mig-1g.35gb"),
                 pod("b", "bob", phase="Pending"),
                 pod("c", "carol", component="hub"),
                 pod("d", "dave", "mig-2g.35gb", profile="gpu-large", cpu="8", mem="64G")]
        got = {p["user"]: p for p in meter.parse_pods({"items": items})}
        self.assertEqual(set(got), {"alice", "dave"})
        self.assertEqual(got["alice"]["profile"], "gpu:mig-1g.35gb")
        self.assertEqual(got["alice"]["gpu_count"], 1)
        self.assertEqual(got["dave"]["profile"], "gpu-large")
        self.assertEqual(got["dave"]["cpu"], 8)
        self.assertEqual(got["dave"]["mem_gb"], 64)

    def test_dcgm(self):
        text = "\n".join([
            '# HELP x', 'DCGM_FI_PROF_GR_ENGINE_ACTIVE{gpu="0",GPU_I_ID="1",pod="jupyter-alice",namespace="jupyterhub"} 0.75',
            'DCGM_FI_DEV_FB_USED{gpu="0",GPU_I_ID="1",pod="jupyter-alice",namespace="jupyterhub"} 2048',
            'DCGM_FI_DEV_GPU_UTIL{gpu="1",GPU_I_ID="2",pod="jupyter-bob",namespace="jupyterhub"} 40',
            'DCGM_FI_PROF_GR_ENGINE_ACTIVE{gpu="2",GPU_I_ID="3",pod="other",namespace="kube-system"} 1',
            'DCGM_FI_PROF_GR_ENGINE_ACTIVE{gpu="3",GPU_I_ID="4",pod="",namespace=""} 0',
        ])
        got = meter.parse_dcgm(text, "jupyterhub")
        self.assertEqual(got["jupyter-alice"], {"busy": 0.75, "mem_gb": 2.0})
        self.assertAlmostEqual(got["jupyter-bob"]["busy"], 0.4)
        self.assertNotIn("other", got)


class Recording(Base):
    def test_idempotent_and_counted(self):
        self.run_minutes([pod("a", "alice", "mig-1g.35gb")], T0, 90)
        rep = meter.usage(self.db, "2026-09", CFG, now=T0 + 90 * 60)
        alice = rep["users"][0]
        self.assertEqual(alice["items"]["gpu:mig-1g.35gb"]["hours"], 1.5)
        self.assertEqual(alice["items"]["cpu"]["hours"], 6.0)          # 4 cores x 1.5 h
        self.assertEqual(alice["items"]["mem_gb"]["hours"], 48.0)      # 32 GB x 1.5 h
        self.assertEqual(alice["sessions"], 1)

    def test_busy_and_idle(self):
        busy = {"jupyter-alice": {"busy": 0.5, "mem_gb": 1.0}}
        self.run_minutes([pod("a", "alice", "mig-1g.35gb")], T0, 30, busy)
        self.run_minutes([pod("a", "alice", "mig-1g.35gb")], T0 + 30 * 60, 30,
                         {"jupyter-alice": {"busy": 0.0, "mem_gb": 1.0}})
        e = meter.usage(self.db, "2026-09", CFG, now=T0 + 3600)["users"][0]
        self.assertAlmostEqual(e["gpu_busy_avg"], 0.25)
        self.assertAlmostEqual(e["idle_gpu_hours"], 0.5)

    def test_backfill_after_meter_outage(self):
        p = [pod("a", "alice", "mig-1g.35gb", started=T0 - 60)]
        self.run_minutes(p, T0, 10)
        # meter down for 30 minutes, pod still running with the same uid
        self.run_minutes(p, T0 + 40 * 60, 10)
        e = meter.usage(self.db, "2026-09", CFG, now=T0 + 50 * 60)["users"][0]
        self.assertAlmostEqual(e["items"]["gpu:mig-1g.35gb"]["hours"], 50 / 60, places=4)
        self.assertAlmostEqual(e["backfilled_hours"], 30 / 60)

    def test_no_backfill_before_pod_started(self):
        self.run_minutes([pod("a", "alice", "mig-1g.35gb", started=T0 - 60)], T0, 5)
        # meter down 30 min; bob's pod started 10 min before the meter came back
        late = [pod("a", "alice", "mig-1g.35gb", started=T0 - 60),
                pod("b", "bob", "mig-1g.35gb", started=T0 + 25 * 60)]
        self.run_minutes(late, T0 + 35 * 60, 1)
        bob = next(e for e in meter.usage(self.db, "2026-09", CFG, now=T0 + 36 * 60)["users"]
                   if e["user"] == "bob")
        # minutes 26..34 backfilled (start minute itself excluded) + 1 sampled
        self.assertEqual(round(bob["gpu_hours"] * 60), 10)

    def test_first_start_backfills_running_pods(self):
        self.run_minutes([pod("a", "alice", "mig-1g.35gb", started=T0 - 20 * 60)], T0, 1)
        e = meter.usage(self.db, "2026-09", CFG, now=T0 + 60)["users"][0]
        self.assertEqual(round(e["gpu_hours"] * 60), 20)

    def test_backfill_capped(self):
        old = meter.BACKFILL_MAX_MINUTES
        meter.BACKFILL_MAX_MINUTES = 5
        try:
            self.run_minutes([pod("a", "alice", "mig-1g.35gb", started=T0 - 3600)], T0, 1)
        finally:
            meter.BACKFILL_MAX_MINUTES = old
        e = meter.usage(self.db, "2026-09", CFG, now=T0 + 60)["users"][0]
        self.assertEqual(round(e["gpu_hours"] * 60), 6)

    def test_pending_not_billed(self):
        self.run_minutes([pod("a", "alice", "mig-1g.35gb", phase="Pending")], T0, 10)
        self.assertEqual(meter.usage(self.db, "2026-09", CFG, now=T0 + 600)["users"], [])


class Reports(Base):
    def test_rates_by_month(self):
        self.assertEqual(meter.rates_for(CFG, "2026-08"), {"gpu:mig-1g.35gb": 1.0, "cpu": 0.5})
        self.assertEqual(meter.rates_for(CFG, "2026-09"), {"gpu:mig-1g.35gb": 2.0, "gpu:mig-2g.35gb": 4.0})
        self.assertEqual(meter.rates_for(CFG, "2025-12"), {})

    def test_costs_and_adjustments(self):
        self.run_minutes([pod("a", "alice", "mig-1g.35gb")], T0, 120)
        meter.add_adjustment(self.db, {"user": "alice", "month": "2026-09",
                                       "resource": "gpu:mig-1g.35gb", "hours": -0.5,
                                       "reason": "outage credit", "by": "admin"}, CFG)
        rep = meter.usage(self.db, "2026-09", CFG, now=T0 + 7200)
        e = rep["users"][0]
        self.assertEqual(e["items"]["gpu:mig-1g.35gb"]["hours"], 1.5)
        self.assertEqual(e["items"]["gpu:mig-1g.35gb"]["cost"], 3.0)
        self.assertNotIn("cost", e["items"]["cpu"])                   # no CPU rate in Sept
        self.assertEqual(e["cost"], 3.0)
        self.assertEqual(e["gpu_hours"], 1.5)
        self.assertTrue(rep["priced"])
        csv = meter.export_csv(self.db, "2026-09", CFG)
        self.assertIn("alice,gpu:mig-1g.35gb", csv)
        self.assertIn("includes adjustments", csv)

    def test_month_boundary_in_timezone(self):
        cfg = dict(CFG, timezone="Africa/Harare")     # UTC+2
        # 2026-09-30 23:30 UTC = 2026-10-01 01:30 Harare
        t = dt.datetime(2026, 9, 30, 23, 30, tzinfo=UTC).timestamp()
        self.run_minutes([pod("a", "alice", "mig-1g.35gb", started=t - 60)], t, 10)
        self.assertEqual(meter.usage(self.db, "2026-09", cfg, now=t + 600)["users"], [])
        self.assertEqual(len(meter.usage(self.db, "2026-10", cfg, now=t + 600)["users"]), 1)

    def test_budget_and_check(self):
        self.run_minutes([pod("a", "alice", "mig-1g.35gb")], T0, 60)
        now = T0 + 3600
        self.assertIsNone(meter.budget_of(self.db, "alice", CFG))
        meter.set_budget(self.db, {"user": "*", "gpu_hours": 10, "by": "admin"})
        self.assertEqual(meter.budget_of(self.db, "alice", CFG), 10)
        meter.set_budget(self.db, {"user": "alice", "gpu_hours": 1, "by": "admin"})
        self.assertEqual(meter.budget_of(self.db, "alice", CFG), 1)
        month, used = meter.gpu_used_this_month(self.db, "alice", CFG, now=now)
        self.assertEqual((month, used), ("2026-09", 1.0))
        meter.set_budget(self.db, {"user": "alice", "gpu_hours": None, "by": "admin"})
        self.assertIsNone(meter.budget_of(self.db, "alice", CFG))     # latest row wins

    def test_gaps_reported(self):
        p = [pod("a", "alice", "mig-1g.35gb", started=T0 - 60)]
        self.run_minutes(p, T0, 5)
        self.run_minutes(p, T0 + 20 * 60, 5)
        st = meter.meter_status(self.db, "2026-09", CFG, now=T0 + 25 * 60 + 5)
        self.assertEqual(st["gap_minutes"], 15)
        self.assertEqual(len(st["gaps"]), 1)
        self.assertEqual(st["backfilled_minutes"], 15)

    def test_detail(self):
        self.run_minutes([pod("a", "alice", "mig-1g.35gb")], T0, 30)
        self.run_minutes([pod("b", "alice", started=T0 + 3540)], T0 + 3600, 30)
        d = meter.user_detail(self.db, "alice", "2026-09", CFG, now=T0 + 5400)
        self.assertEqual(len(d["sessions"]), 2)
        self.assertEqual(d["daily"][0]["date"], "2026-09-10")
        self.assertEqual(d["daily"][0]["gpu_hours"], 0.5)

    def test_validation(self):
        with self.assertRaises(ValueError):
            meter.set_budget(self.db, {"user": "Robert'); DROP", "gpu_hours": 1, "by": "a"})
        with self.assertRaises(ValueError):
            meter.set_budget(self.db, {"user": "alice", "gpu_hours": -1, "by": "a"})
        with self.assertRaises(ValueError):
            meter.add_adjustment(self.db, {"user": "alice", "month": "2026-13", "resource": "cpu",
                                           "hours": 1, "reason": "x", "by": "a"}, CFG)
        with self.assertRaises(ValueError):
            meter.add_adjustment(self.db, {"user": "alice", "month": "2026-09", "resource": "cpu",
                                           "hours": 1, "reason": "", "by": "a"}, CFG)


class Http(Base):
    TOKEN = "t" * 40

    def setUp(self):
        super().setUp()
        path = os.path.join(self.tmp.name, "m.sqlite")
        self.cfg_path = os.path.join(self.tmp.name, "config.json")
        with open(self.cfg_path, "w") as f:
            json.dump(CFG, f)
        self._cfg = meter.CONFIG_FILE
        meter.load_config.__defaults__ = (self.cfg_path,)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0),
                                       meter.make_handler(lambda: meter.connect(path), self.TOKEN))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        meter.load_config.__defaults__ = (self._cfg,)
        super().tearDown()

    def call(self, path, body=None, token=TOKEN):
        req = urllib.request.Request(self.url + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_auth_required(self):
        self.assertEqual(self.call("/api/usage", token="wrong")[0], 401)
        self.assertEqual(self.call("/livez", token="")[0], 200)

    def test_health_goes_stale(self):
        self.assertEqual(self.call("/healthz")[0], 503)          # never sampled
        meter.record(self.db, [], {}, __import__("time").time())
        self.assertEqual(self.call("/healthz")[0], 200)

    def test_budget_roundtrip(self):
        self.assertEqual(self.call("/api/budget", {"user": "alice", "gpu_hours": 5, "by": "admin"})[0], 200)
        code, body = self.call("/api/check/alice")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["budget"], 5)
        self.assertTrue(json.loads(body)["allowed"])
        self.assertEqual(self.call("/api/budget", {"user": "x", "by": "admin"})[0], 400)

    def test_usage_and_csv(self):
        code, body = self.call("/api/usage?month=2026-09")
        self.assertEqual(code, 200)
        self.assertIn("meter", json.loads(body))
        self.assertEqual(self.call("/api/usage?month=bad")[0], 400)
        code, body = self.call("/api/export.csv?month=2026-09")
        self.assertEqual(code, 200)
        self.assertTrue(body.startswith(b"month,user,resource"))


if __name__ == "__main__":
    unittest.main()
