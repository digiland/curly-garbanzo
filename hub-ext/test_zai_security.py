"""Tests for the security assessment fixes (November 2026): the hub's response
headers and cookie flags (chart extraConfig 05-security-headers), and the
lockout limits no longer shown on the sign-in and signup pages.

The headers and cookies go through JupyterHub's own BaseHandler code, so these
check what the hub would actually send. They need the hub's packages:

    pip install jupyterhub pyyaml
    cd hub-ext && python3 -m unittest -v test_zai_security
"""
import os
import types
import unittest
from urllib.parse import urlparse

import yaml
from jupyterhub.handlers.base import BaseHandler
from traitlets.config import Config

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
VALUES = yaml.safe_load(open(os.path.join(REPO, "charts", "jupyterhub-values.yaml")))


def hub_settings():
    """tornado_settings as the hub has them after the chart's extraConfig."""
    c = Config()
    exec(VALUES["hub"]["extraConfig"]["05-security-headers"], {"c": c})
    return c.JupyterHub.tornado_settings


class Recorder:
    """Just enough of a request handler for BaseHandler's header and cookie code."""

    def __init__(self, settings, protocol="http", public_url=None):
        self.settings = dict(settings, public_url=urlparse(public_url) if public_url else None)
        self.request = types.SimpleNamespace(protocol=protocol)
        self.log = types.SimpleNamespace(debug=lambda *a, **k: None)
        self.content_security_policy = "frame-ancestors 'none'"
        self.headers, self.cookies = {}, {}

    def set_header(self, name, value):
        self.headers[name] = value

    def get_content_type(self):
        return "text/html"

    def set_secure_cookie(self, key, value, **kwargs):
        self.cookies[key] = kwargs

    set_cookie = set_secure_cookie


class Headers(unittest.TestCase):
    def test_every_hub_page_sends_the_security_headers(self):
        r = Recorder(hub_settings())
        BaseHandler.set_default_headers(r)
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertIn("camera=()", r.headers["Permissions-Policy"])
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy-Report-Only"])

    def test_jupyterhubs_enforced_policy_is_kept(self):
        # Ours is report-only, so it must not replace the one JupyterHub enforces.
        r = Recorder(hub_settings())
        BaseHandler.set_default_headers(r)
        self.assertEqual(r.headers["Content-Security-Policy"], "frame-ancestors 'none'")

    def test_report_only_policy_allows_what_the_pages_load(self):
        csp = hub_settings()["headers"]["Content-Security-Policy-Report-Only"]
        for source in ("https://fonts.googleapis.com", "https://fonts.gstatic.com", "https://www.google.com"):
            self.assertIn(source, csp)


class Cookies(unittest.TestCase):
    def test_cookies_are_secure_behind_the_tunnel(self):
        # Cloudflare hands the hub plain http; on its own JupyterHub would then
        # leave Secure off.
        r = Recorder(hub_settings(), protocol="http")
        BaseHandler._set_cookie(r, "jupyterhub-session-id", "x", encrypted=False)
        BaseHandler._set_cookie(r, "jupyterhub-hub-login", "x")
        for kwargs in r.cookies.values():
            self.assertTrue(kwargs["secure"])
            self.assertEqual(kwargs["samesite"], "Lax")
            self.assertTrue(kwargs["httponly"])

    def test_without_the_settings_jupyterhub_leaves_secure_off(self):
        # What the assessors saw, so the test above is testing something.
        r = Recorder({}, protocol="http")
        BaseHandler._set_cookie(r, "jupyterhub-session-id", "x", encrypted=False)
        self.assertNotIn("secure", r.cookies["jupyterhub-session-id"])

    def test_xsrf_cookie_gets_the_same_flags(self):
        # JupyterHub copies cookie_options to the _xsrf cookie when the hub
        # has no xsrf_cookie_kwargs of its own; the chart must not set any.
        ts = hub_settings()
        self.assertFalse(ts.get("xsrf_cookie_kwargs"))
        self.assertNotIn("httponly", ts["cookie_options"])   # pages read _xsrf


class LockoutNotShown(unittest.TestCase):
    def test_no_page_states_the_lockout_limits(self):
        for name in ("native-login.html", "signup.html"):
            page = open(os.path.join(REPO, "templates", name)).read()
            self.assertNotIn("failed attempts", page, name)
        chart = VALUES["hub"]["extraConfig"]["00-nativeauth"]
        self.assertNotIn("failed attempts", chart)

    def test_the_lockout_itself_is_still_on(self):
        auth = VALUES["hub"]["config"]["NativeAuthenticator"]
        self.assertEqual(auth["allowed_failed_logins"], 5)
        self.assertEqual(auth["seconds_before_next_try"], 900)


if __name__ == "__main__":
    unittest.main()
