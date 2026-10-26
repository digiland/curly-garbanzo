"""Who may create and delete endpoints: administrators for anyone, members of
serverless.creators_group for themselves only.

Runs against the real gateway handlers, with a fake hub (token -> user) and a
fake Kubernetes (every user has a home volume):

    cd serverless && python3 -m unittest -v test_gateway_endpoints
"""

import json
import os
import tempfile
import unittest

os.environ.setdefault("HUB_TOKEN", "hub-secret")

from tornado.testing import AsyncHTTPTestCase   # noqa: E402
from tornado.web import Application             # noqa: E402

import gateway                                  # noqa: E402

CATALOG = """
serverless:
  enabled: true
  max_gpus: 2
  max_workers_per_endpoint: 3
  creators_group: endpoint-creators
  max_endpoints_per_user: 2
sizes:
  - {slug: cpu, name: CPU only}
templates:
  - {slug: pytorch, name: PyTorch}
"""

USERS = {   # token -> what the hub's /user answers, as Hub.whoami models it
    "t-admin": {"name": "admin", "admin": True, "serverless": True, "groups": []},
    "t-member": {"name": "tendai", "admin": False, "serverless": True, "groups": ["endpoint-creators"]},
    "t-other": {"name": "rudo", "admin": False, "serverless": True, "groups": []},
}


class FakeHub:
    async def whoami(self, token):
        return USERS.get(token)


class FakeK8s:
    async def home_pvc(self, owner):
        return f"claim-{owner}"


class Base(AsyncHTTPTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        path = os.path.join(self.tmp, "catalog.yaml")
        with open(path, "w") as f:
            f.write(CATALOG)
        self.gw = gateway.Gateway(store=gateway.Store(os.path.join(self.tmp, "gw.sqlite")),
                                  k8s=FakeK8s(), hub=FakeHub(), catalog=gateway.Catalog(path))
        super().setUp()

    def get_app(self):
        # The public API, and the hub-only port's API mounted under /hub-port.
        hub = gateway._api_routes({"gw": self.gw, "via_hub": True})
        return Application(gateway._api_routes({"gw": self.gw}) +
                           [("/hub-port" + r[0], r[1], r[2]) for r in hub])

    def call(self, method, path, token=None, body=None, hub_user=None, groups=None, admin=False):
        headers = {"Content-Type": "application/json"}
        if hub_user:
            path = "/hub-port" + path
            headers.update({"Authorization": f"token {gateway.HUB_TOKEN}", "X-Zai-User": hub_user,
                            "X-Zai-Admin": "1" if admin else "0", "X-Zai-Groups": json.dumps(groups or [])})
        elif token:
            headers["Authorization"] = f"Bearer {token}"
        r = self.fetch(path, method=method, headers=headers,
                       body=json.dumps(body) if body is not None else (b"" if method == "POST" else None),
                       raise_error=False)
        return r.code, (json.loads(r.body) if r.body and r.body.startswith(b"{") else {}), r.reason

    def create(self, token, **kw):
        body = {"name": "echo", "size": "cpu", "workdir": "endpoints/echo"}
        body.update(kw)
        return self.call("POST", "/v2/endpoints", token, body)


class Create(Base):
    def test_member_creates_their_own_without_naming_an_owner(self):
        code, ep, _ = self.create("t-member")
        self.assertEqual(code, 201)
        self.assertEqual(ep["owner"], "tendai")
        row = self.gw.db.one("SELECT created_by FROM endpoints WHERE id=?", (ep["id"],))
        self.assertEqual(row["created_by"], "tendai")

    def test_member_may_name_themselves(self):
        self.assertEqual(self.create("t-member", owner="tendai")[0], 201)

    def test_member_cannot_create_for_someone_else(self):
        code, _, reason = self.create("t-member", owner="rudo")
        self.assertEqual(code, 403)
        self.assertIn("only for yourself", reason)
        self.assertEqual(self.gw.db.one("SELECT COUNT(*) FROM endpoints")[0], 0)

    def test_non_member_is_refused_and_told_how_to_get_access(self):
        code, _, reason = self.create("t-other")
        self.assertEqual(code, 403)
        self.assertIn("endpoint-creators", reason)

    def test_member_limit(self):
        self.assertEqual(self.create("t-member", name="a")[0], 201)
        self.assertEqual(self.create("t-member", name="b")[0], 201)
        code, _, reason = self.create("t-member", name="c")
        self.assertEqual(code, 403)
        self.assertIn("most allowed", reason)

    def test_admin_is_not_limited_and_creates_for_anyone(self):
        for n in "abc":
            code, ep, _ = self.create("t-admin", owner="rudo", name=n)
            self.assertEqual(code, 201)
            self.assertEqual(ep["owner"], "rudo")

    def test_caps_still_apply_to_members(self):
        code, _, reason = self.create("t-member", max_workers=7)
        self.assertEqual(code, 400)
        self.assertIn("max_workers: 1-3", reason)

    def test_no_group_configured_means_admins_only(self):
        self.gw.catalog.creators_group = ""
        code, _, reason = self.create("t-member")
        self.assertEqual(code, 403)
        self.assertIn("only administrators", reason)

    def test_hub_page_passes_groups(self):
        body = {"name": "echo", "size": "cpu", "workdir": "endpoints/echo", "owner": "tendai"}
        code, ep, _ = self.call("POST", "/v2/endpoints", body=body, hub_user="tendai", groups=["endpoint-creators"])
        self.assertEqual(code, 201)
        code, _, _ = self.call("POST", "/v2/endpoints", body=dict(body, name="x"), hub_user="tendai", groups=[])
        self.assertEqual(code, 403)

    def test_hub_page_rejects_a_malformed_groups_header(self):
        r = self.fetch("/hub-port/v2/endpoints", method="POST", body="{}", raise_error=False, headers={
            "Authorization": f"token {gateway.HUB_TOKEN}", "X-Zai-User": "tendai", "X-Zai-Groups": "not json"})
        self.assertEqual(r.code, 400)


class Delete(Base):
    def test_member_deletes_their_own(self):
        _, ep, _ = self.create("t-member")
        self.assertEqual(self.call("DELETE", f"/v2/endpoints/{ep['id']}", "t-member")[0], 200)

    def test_member_cannot_see_or_delete_someone_elses(self):
        _, ep, _ = self.create("t-admin", owner="rudo")
        self.assertEqual(self.call("DELETE", f"/v2/endpoints/{ep['id']}", "t-member")[0], 404)

    def test_owner_outside_the_group_still_asks_an_admin(self):
        _, ep, _ = self.create("t-admin", owner="rudo")
        code, _, reason = self.call("DELETE", f"/v2/endpoints/{ep['id']}", "t-other")
        self.assertEqual(code, 403)
        self.assertIn("administrator", reason)

    def test_deleting_frees_a_place_under_the_limit(self):
        _, a, _ = self.create("t-member", name="a")
        self.create("t-member", name="b")
        self.assertEqual(self.create("t-member", name="c")[0], 403)
        self.call("DELETE", f"/v2/endpoints/{a['id']}", "t-member")
        self.assertEqual(self.create("t-member", name="c")[0], 201)


if __name__ == "__main__":
    unittest.main()
