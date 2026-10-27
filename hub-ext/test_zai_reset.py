"""Tests for hub-ext/zai_reset.py: self-service password reset by email.

The module runs against a real NativeAuthenticator, the hub's tables in a
throwaway database, and zai_2fa's real code check. No email is sent: the
steps are tested directly and the email text checked. It needs the hub's
packages:

    pip install jupyterhub jupyterhub-nativeauthenticator==1.2.0 pyyaml
    cd hub-ext && python3 -m unittest -v test_zai_reset
"""
import base64
import os
import time
import types
import unittest

import sqlalchemy as sa
import yaml
from jinja2 import DictLoader, Environment
from jupyterhub import orm
from nativeauthenticator import NativeAuthenticator
from nativeauthenticator.orm import UserInfo
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from traitlets.config import Config

import zai_2fa  # noqa: E402  (hub-ext is this file's own directory)
import zai_reset  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
VALUES = yaml.safe_load(open(os.path.join(REPO, "charts", "jupyterhub-values.yaml")))
OLD, NEW = "old-password-123", "a-brand-new-passphrase"
SMTP = {"ZAI_PUBLIC_URL": "https://compute.example", "MAIL_FROM": "no-reply@compute.example",
        "SMTP_HOST": "smtp.example", "SMTP_USERNAME": "u", "SMTP_PASSWORD": "p"}


def code_for(info, at=None):
    secret = info.otp_secret.upper()
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    return zai_2fa._code(key, int((at or time.time()) // 30))


class Case(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://")
        orm.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.auth = NativeAuthenticator(db=self.db, ask_email_on_signup=True, open_signup=False,
                                        minimum_password_length=12, check_common_password=True)
        UserInfo.__table__.create(engine, checkfirst=True)
        zai_2fa.configure(Config())
        zai_2fa._last_used.clear()
        zai_reset._rs_recent.clear()
        zai_reset._rs_ready.clear()
        self.config = Config()
        self.config.JupyterHub.template_vars = {}
        zai_reset.configure(self.config, env=SMTP)

    def user(self, name="tendai", email="Tendai@Zai.example", approved=True, two_factor=False):
        info = self.auth.create_user(name, OLD, email=email)
        info.is_authorized = approved
        if two_factor:
            info.has_2fa, info.otp_secret = True, zai_2fa.new_secret()
        self.db.add(orm.User(name=name))
        self.db.commit()
        return info

    def ask(self, email="tendai@zai.example", visitor="1.2.3.4", now=None):
        return zai_reset._rs_request(self.db, email, visitor, now)

    def link(self, **kw):
        [(_, _, link)] = self.ask(**kw)
        return link

    def complete(self, link, password=NEW, confirm=None, code="", now=None):
        return zai_reset._rs_complete(self.db, self.auth, link, password,
                                      password if confirm is None else confirm, code, now)

    def rows(self):
        return [dict(r._mapping) for r in self.db.execute(sa.select(zai_reset._rs_table))]


class Settings(Case):
    def test_on_with_smtp(self):
        self.assertTrue(zai_reset._RS["on"])
        self.assertEqual(zai_reset._RS["smtp"]["port"], 587)
        self.assertEqual(zai_reset._RS["smtp"]["security"], "starttls")

    def test_on_with_resend_and_the_approval_from_address(self):
        zai_reset.configure(Config(), env={"ZAI_PUBLIC_URL": "https://c.example", "RESEND_API_KEY": "re_x",
                                           "APPROVAL_FROM_EMAIL": "hi@c.example"})
        self.assertTrue(zai_reset._RS["on"])
        self.assertEqual(zai_reset._RS["from"], "hi@c.example")

    def test_off_without_what_it_needs(self):
        for env in ({},
                    dict(SMTP, ZAI_PUBLIC_URL=""),
                    dict(SMTP, ZAI_PUBLIC_URL="http://compute.example"),      # not https
                    dict(SMTP, MAIL_FROM=""),
                    dict(SMTP, SMTP_HOST=""),
                    dict(SMTP, SMTP_SECURITY="none"),                         # plain text to another machine
                    dict(SMTP, SMTP_SECURITY="tls1.0")):
            zai_reset.configure(Config(), env=env)
            self.assertFalse(zai_reset._RS["on"], env)

    def test_ssl_port_and_a_local_test_server(self):
        zai_reset.configure(Config(), env=dict(SMTP, SMTP_SECURITY="ssl"))
        self.assertEqual(zai_reset._RS["smtp"]["port"], 465)
        zai_reset.configure(Config(), env=dict(SMTP, ZAI_PUBLIC_URL="http://127.0.0.1:8090",
                                               SMTP_HOST="127.0.0.1", SMTP_PORT="1025", SMTP_SECURITY="none"))
        self.assertTrue(zai_reset._RS["on"])

    def test_pages_are_registered(self):
        routes = [h[0] for h in self.config.JupyterHub.extra_handlers]
        self.assertEqual(routes, ["/forgot", r"/reset/([A-Za-z0-9_-]{20,100})"])
        self.assertTrue(self.config.JupyterHub.template_vars["zai_reset_on"](None))


class Requesting(Case):
    def test_an_approved_account_gets_one_link(self):
        self.user()
        [(name, to, link)] = self.ask()
        self.assertEqual((name, to), ("tendai", "Tendai@Zai.example"))
        self.assertGreaterEqual(len(link), 40)

    def test_email_is_matched_without_case_or_spaces(self):
        self.user()
        self.assertEqual(len(self.ask(email="  TENDAI@zai.EXAMPLE ")), 1)

    def test_nothing_for_unknown_unapproved_or_malformed(self):
        self.user(approved=False)
        self.assertEqual(self.ask(), [])
        self.assertEqual(self.ask(email="nobody@zai.example"), [])
        self.assertEqual(self.ask(email="not an email"), [])

    def test_only_a_hash_is_stored(self):
        self.user()
        link = self.link()
        [row] = self.rows()
        self.assertNotEqual(row["link_hash"], link)
        self.assertNotIn(link, str(row))

    def test_a_new_link_cancels_the_old_one(self):
        self.user()
        first, second = self.link(), self.link()
        self.assertIsNone(zai_reset._rs_find(self.db, first))
        self.assertEqual(zai_reset._rs_find(self.db, second), "tendai")

    def test_three_links_per_address_per_hour(self):
        self.user()
        now = time.time()
        for i in range(3):
            self.assertEqual(len(self.ask(visitor=f"10.0.0.{i}", now=now)), 1)
        self.assertEqual(self.ask(visitor="10.0.0.9", now=now), [])
        self.assertEqual(len(self.ask(visitor="10.0.0.9", now=now + 3601)), 1)

    def test_twenty_requests_per_visitor_per_hour(self):
        now = time.time()
        for i in range(20):
            self.ask(email=f"x{i}@zai.example", now=now)
        self.user()
        self.assertEqual(self.ask(now=now), [])
        self.assertEqual(len(self.ask(visitor="5.6.7.8", now=now)), 1)


class Completing(Case):
    def test_sets_the_password_once(self):
        info = self.user()
        link = self.link()
        self.assertEqual(self.complete(link), "tendai")
        self.assertTrue(info.is_valid_password(NEW))
        self.assertFalse(info.is_valid_password(OLD))
        with self.assertRaises(ValueError):
            self.complete(link, password="another-good-passphrase")
        self.assertTrue(info.is_valid_password(NEW))

    def test_link_expires_after_30_minutes(self):
        self.user()
        now = time.time()
        link = self.link(now=now)
        self.assertEqual(zai_reset._rs_find(self.db, link, now + 29 * 60), "tendai")
        with self.assertRaises(ValueError):
            self.complete(link, now=now + 31 * 60)

    def test_unknown_link(self):
        with self.assertRaises(ValueError):
            self.complete("x" * 43)

    def test_password_rules_keep_the_link_usable(self):
        self.user()
        link = self.link()
        for password, confirm in (("short", "short"), ("1qaz2wsx3edc", "1qaz2wsx3edc"),
                                  (NEW, NEW + "x")):
            with self.assertRaises(ValueError, msg=password):
                self.complete(link, password, confirm)
        self.assertEqual(self.complete(link), "tendai")

    def test_signs_other_devices_out_and_lifts_the_lockout(self):
        self.user()
        before = orm.User.find(self.db, "tendai").cookie_id
        for _ in range(5):
            self.auth.add_login_attempt("tendai")
        self.assertTrue(self.auth.is_blocked("tendai"))
        self.complete(self.link())
        self.assertNotEqual(orm.User.find(self.db, "tendai").cookie_id, before)
        self.assertFalse(self.auth.is_blocked("tendai"))

    def test_racing_resets_use_the_link_once(self):
        self.user()
        link = self.link()
        self.complete(link)
        with self.assertRaises(ValueError):
            self.complete(link, password="yet-another-passphrase")


class TwoFactor(Case):
    def test_code_is_required_and_checked(self):
        info = self.user(two_factor=True)
        link = self.link()
        for code in ("", "000000"):
            with self.assertRaises(ValueError, msg=code):
                self.complete(link, code=code)
        self.assertTrue(info.is_valid_password(OLD))
        self.complete(link, code=code_for(info))
        self.assertTrue(info.is_valid_password(NEW))

    def test_five_wrong_codes_cancel_the_link(self):
        info = self.user(two_factor=True)
        link = self.link()
        for _ in range(5):
            with self.assertRaises(ValueError):
                self.complete(link, code="000000")
        with self.assertRaises(ValueError):
            self.complete(link, code=code_for(info))
        self.assertTrue(info.is_valid_password(OLD))


class RequestLog(Case):
    def test_reset_links_are_hidden_in_the_request_log(self):
        import jupyterhub.log
        scrub = jupyterhub.log._scrub_uri
        self.assertEqual(scrub("/hub/reset/TM0vsDwm3fS_abc-123"), "/hub/reset/[secret]")
        self.assertEqual(scrub("/hub/home"), "/hub/home")
        zai_reset.configure(Config(), env=SMTP)                   # wrapping twice is harmless
        self.assertEqual(jupyterhub.log._scrub_uri("/hub/reset/abc"), "/hub/reset/[secret]")


class Emails(Case):
    def test_reset_email_links_to_the_configured_site(self):
        subject, text = zai_reset._rs_reset_email("tendai", "LINK123")
        self.assertEqual(subject, "Reset your ZAI Compute password")
        self.assertIn("https://compute.example/hub/reset/LINK123", text)
        self.assertIn("30 minutes", text)

    def test_changed_email_mentions_caimex(self):
        subject, text = zai_reset._rs_changed_email("tendai", 0)
        self.assertIn("password was changed", subject)
        self.assertIn("Caimex password has not changed", text)
        self.assertIn("01 Jan 1970 00:00 UTC", text)


class Pages(unittest.TestCase):
    def render(self, name, **variables):
        env = Environment(autoescape=True, loader=DictLoader({
            "page.html": "{% block main %}{% endblock %}{% block script %}{% endblock %}",
            "zai-panel.html": "",
            name: open(os.path.join(REPO, "templates", name)).read()}))
        return env.get_template(name).render(base_url="/hub/", xsrf="X", **variables)

    def test_login_links_to_reset_only_when_on(self):
        on = self.render("native-login.html", zai_reset_on=True, login_url="/hub/login", next="", username="")
        self.assertIn('href="/hub/forgot"', on)
        off = self.render("native-login.html", zai_reset_on=False, login_url="/hub/login", next="", username="")
        self.assertNotIn("/hub/forgot", off)
        self.assertIn("An administrator can reset it", off)

    def test_forgot_page_says_the_same_thing_for_everyone(self):
        page = self.render("forgot.html", sent=True, email="<b>x</b>@zai.example")
        self.assertIn("If an account uses", page)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", page)
        form = self.render("forgot.html", sent=False, email="")
        self.assertIn('name="email"', form)
        self.assertIn('action="/hub/forgot"', form)

    def test_reset_page_states(self):
        form = self.render("reset.html", link="L" * 43, username="tendai", needs_code=False, min_length=12)
        self.assertIn('action="/hub/reset/' + "L" * 43, form)
        self.assertNotIn('name="code"', form)
        self.assertIn('name="code"', self.render("reset.html", link="L", username="tendai", needs_code=True, min_length=12))
        self.assertIn("This link no longer works", self.render("reset.html", link="L", username=None))
        self.assertIn("Password changed", self.render("reset.html", link="L", username="tendai", done=True))

    def test_the_hub_loads_it_after_two_factor_and_ships_it(self):
        startup = VALUES["hub"]["extraConfig"]["03-zai-platform"]
        self.assertLess(startup.index("zai_2fa.configure(c)"), startup.index("zai_reset.configure(c)"))
        deploy = open(os.path.join(REPO, "scripts", "05-jupyterhub.sh")).read()
        self.assertIn("--from-file=hub-ext/zai_reset.py", deploy)
        self.assertIn("hub-ext/zai_reset.py", deploy.split("ZAI_SUM=")[1].split("\n")[0])
        for key in ("ZAI_PUBLIC_URL", "MAIL_FROM", "SMTP_HOST", "SMTP_PASSWORD"):
            self.assertEqual(VALUES["hub"]["extraEnv"][key]["valueFrom"]["secretKeyRef"]["name"], "outgoing-mail")


if __name__ == "__main__":
    unittest.main()
