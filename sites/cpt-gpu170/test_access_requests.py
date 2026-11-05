"""Tests for the request-access form config in values.yaml (07-access-requests).

The code under test is the text of that config block, executed here exactly as
the hub executes it, against a real NativeAuthenticator and its real signup
handler. It needs the hub's packages:

    pip install jupyterhub jupyterhub-nativeauthenticator==1.2.0 pyyaml
    cd sites/cpt-gpu170 && python3 -m unittest -v test_access_requests
"""
import datetime
import json
import os
import re
import types
import unittest

import yaml
from jinja2 import DictLoader, Environment
from jupyterhub import orm
from nativeauthenticator import NativeAuthenticator
from nativeauthenticator.handlers import SignUpHandler
from nativeauthenticator.orm import UserInfo
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tornado import web
from traitlets.config import Config

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
BLOCK = yaml.safe_load(open(os.path.join(HERE, "values.yaml")))["hub"]["extraConfig"]["07-access-requests"]

# The config block patches these in place. Restore them around each test.
_ORIGINALS = {
    (SignUpHandler, "post"): SignUpHandler.post,
    (SignUpHandler, "render_template"): SignUpHandler.render_template,
    (NativeAuthenticator, "delete_user"): NativeAuthenticator.delete_user,
    (NativeAuthenticator, "get_handlers"): NativeAuthenticator.get_handlers,
}

INDIVIDUAL = {
    "applicant_type": "individual", "full_name": "Tendai Moyo", "phone": "+263 77 123 4567",
    "country": "Zimbabwe", "sector": "Health", "project_name": "Clinic triage",
    "problem": "Rural clinics cannot triage quickly.", "current_solution": "Paper forms.",
    "approach": "A small speech model in Shona.", "gpu_hours": "30", "gpu_size": "standard",
    "duration_months": "6", "consent": "yes"}
ORGANISATION = dict(
    INDIVIDUAL, applicant_type="organisation", org_name="Kumusha Labs (Pvt) Ltd",
    org_description="Language tools for southern Africa.", org_type="Startup",
    org_registration="12345/2021", org_founded="2021-03-15", contact2_name="Rudo Ncube",
    contact2_email="rudo@kumusha.example", contact2_phone="+263712345678")
ACCOUNT = {"username": "tendai", "email": "tendai@zai.example",
           "signup_password": "tendai-password-1", "signup_password_confirmation": "tendai-password-1"}


def restore():
    for (owner, name), original in _ORIGINALS.items():
        setattr(owner, name, original)


class Case(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        restore()
        engine = create_engine("sqlite://")
        orm.Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.config = Config()
        self.config.JupyterHub.template_vars = {}
        self.ns = {"c": self.config}
        exec(BLOCK, self.ns)
        self.auth = NativeAuthenticator(db=self.db, ask_email_on_signup=True,
                                        open_signup=False, minimum_password_length=12)
        NativeAuthenticator.get_handlers(self.auth, types.SimpleNamespace(db=self.db))
        # As JupyterHub builds a page: callables are called with the user.
        self.page_vars = {k: (v(None) if callable(v) else v)
                          for k, v in self.config.JupyterHub.template_vars.items()}

    def tearDown(self):
        restore()

    def validate(self, fields):
        return self.ns["_ar_validate"](lambda name: str(fields.get(name, "")).strip())

    async def submit(self, fields):
        """POST the signup form. Returns the handler, with what it rendered."""
        handler = object.__new__(SignUpHandler)
        handler.application = types.SimpleNamespace(
            settings={"authenticator": self.auth, "db": self.db})
        handler.request = types.SimpleNamespace(headers={}, connection=None)
        handler.rendered, handler.finished = [], []
        handler.get_body_argument = lambda name, default="", strip=True: (
            str(fields.get(name, default)).strip() if strip else str(fields.get(name, default)))
        handler.finish = handler.finished.append

        async def render(_self, name, *args, **ns):
            # Stands in for the hub's page renderer; the wrapper under test
            # still decides what the template is given.
            handler.rendered.append((name, ns))
            return "page"
        self.ns["_ar_signup_render"] = render
        await SignUpHandler.post(handler)
        return handler

    def stored(self, username):
        return self.page_vars["access_request"](username)


class Validation(Case):
    def test_complete_individual(self):
        answers, problems = self.validate(INDIVIDUAL)
        self.assertEqual(problems, [])
        self.assertEqual(answers["gpu_hours"], 30)
        self.assertEqual(answers["duration_months"], 6)
        self.assertNotIn("org_name", answers)

    def test_complete_organisation(self):
        answers, problems = self.validate(ORGANISATION)
        self.assertEqual(problems, [])
        self.assertEqual(answers["org_founded"], "2021-03-15")

    def test_individual_is_not_asked_for_organisation_details(self):
        # Even if a tampered page sends them, they are not kept.
        answers, problems = self.validate(dict(INDIVIDUAL, org_name="Sneaky Ltd"))
        self.assertEqual(problems, [])
        self.assertNotIn("org_name", answers)

    def test_organisation_must_answer_every_organisation_question(self):
        for field in ("org_name", "org_description", "org_type", "org_registration",
                      "org_founded", "contact2_name", "contact2_email", "contact2_phone"):
            with self.subTest(field=field):
                _, problems = self.validate(dict(ORGANISATION, **{field: ""}))
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("is required", problems[0])

    def test_everyone_must_answer_every_shared_question(self):
        for field in ("full_name", "phone", "country", "sector", "project_name", "problem",
                      "current_solution", "approach", "gpu_hours", "gpu_size", "duration_months"):
            with self.subTest(field=field):
                _, problems = self.validate(dict(INDIVIDUAL, **{field: ""}))
                self.assertEqual(len(problems), 1, problems)

    def test_applicant_type_is_required_and_fixed(self):
        for value in ("", "company", "Individual"):
            with self.subTest(value=value):
                _, problems = self.validate(dict(INDIVIDUAL, applicant_type=value))
                self.assertIn("individual or an organisation", problems[0])

    def test_consent_is_required(self):
        for value in ("", "no", "on"):
            with self.subTest(value=value):
                _, problems = self.validate(dict(INDIVIDUAL, consent=value))
                self.assertEqual(problems, ["Tick the box to agree to how this information is used."])

    def test_choices_must_come_from_the_lists(self):
        for field, value in (("sector", "Mining"), ("gpu_size", "huge")):
            with self.subTest(field=field):
                _, problems = self.validate(dict(INDIVIDUAL, **{field: value}))
                self.assertIn("choose one of the options", problems[0])
        _, problems = self.validate(dict(ORGANISATION, org_type="Individual or Sole Practitioner"))
        self.assertIn("choose one of the options", problems[0])

    def test_other_sector_must_be_named(self):
        _, problems = self.validate(dict(INDIVIDUAL, sector="Other"))
        self.assertEqual(problems, ["Sector (other) is required."])
        answers, problems = self.validate(dict(INDIVIDUAL, sector="Other", sector_other="Mining"))
        self.assertEqual((problems, answers["sector_other"]), ([], "Mining"))

    def test_gpu_hours_must_be_a_sensible_whole_number(self):
        for value in ("0", "-5", "2.5", "lots", "100001", "1e3", "\u00b2", "\u0663\u0660"):
            with self.subTest(value=value):
                _, problems = self.validate(dict(INDIVIDUAL, gpu_hours=value))
                self.assertIn("whole number from 1 to 100000", problems[0])

    def test_duration_is_one_to_sixty_months(self):
        for value in ("0", "61", "six"):
            with self.subTest(value=value):
                _, problems = self.validate(dict(INDIVIDUAL, duration_months=value))
                self.assertIn("whole number from 1 to 60", problems[0])

    def test_phone_numbers(self):
        for good in ("+263 77 123 4567", "0771234567", "+1 (415) 555-0100"):
            self.assertEqual(self.validate(dict(INDIVIDUAL, phone=good))[1], [], good)
        for bad in ("call me", "123", "+263-<script>", "12345678901234567890123"):
            with self.subTest(bad=bad):
                self.assertIn("phone number", self.validate(dict(INDIVIDUAL, phone=bad))[1][0])

    def test_secondary_contact_email(self):
        for bad in ("rudo", "rudo@", "rudo@kumusha", "a b@c.example"):
            with self.subTest(bad=bad):
                _, problems = self.validate(dict(ORGANISATION, contact2_email=bad))
                self.assertIn("email address", problems[0])

    def test_founding_date(self):
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        for bad, why in ((tomorrow, "cannot be in the future"), ("15/03/2021", "must be a date"),
                         ("1700-01-01", "must be a real date")):
            with self.subTest(bad=bad):
                _, problems = self.validate(dict(ORGANISATION, org_founded=bad))
                self.assertIn(why, problems[0])

    def test_length_limits(self):
        for field, limit in (("full_name", 120), ("problem", 1000), ("country", 80)):
            with self.subTest(field=field):
                self.assertEqual(self.validate(dict(INDIVIDUAL, **{field: "x" * limit}))[1], [])
                _, problems = self.validate(dict(INDIVIDUAL, **{field: "x" * (limit + 1)}))
                self.assertIn(f"{limit} characters or fewer", problems[0])

    def test_every_problem_is_reported_at_once(self):
        _, problems = self.validate({"applicant_type": "individual"})
        self.assertEqual(len(problems), 12)       # eleven questions and the consent box


class Signup(Case):
    async def test_complete_application_creates_the_account_and_stores_it(self):
        handler = await self.submit(dict(ACCOUNT, **INDIVIDUAL))
        self.assertIsNotNone(UserInfo.find(self.db, "tendai"))
        self.assertFalse(UserInfo.find(self.db, "tendai").is_authorized)
        self.assertEqual(handler.rendered[-1][1]["alert"], "alert-info")
        request = self.stored("tendai")
        self.assertEqual((request["type"], request["who"], request["gpu_hours"]),
                         ("Individual", "Tendai Moyo", 30))

    async def test_organisation_is_listed_under_its_name(self):
        await self.submit(dict(ACCOUNT, **ORGANISATION))
        request = self.stored("tendai")
        self.assertEqual((request["type"], request["who"]), ("Organisation", "Kumusha Labs (Pvt) Ltd"))
        self.assertIn(("Secondary contact: email", "rudo@kumusha.example"), request["rows"])
        self.assertIn(("GPU size", "GPU Standard"), request["rows"])

    async def test_incomplete_application_creates_no_account(self):
        handler = await self.submit(dict(ACCOUNT, **dict(INDIVIDUAL, phone="", consent="")))
        self.assertIsNone(UserInfo.find(self.db, "tendai"))
        self.assertIsNone(self.stored("tendai"))
        name, ns = handler.rendered[-1]
        self.assertEqual((name, ns["alert"]), ("signup.html", "alert-danger"))
        self.assertEqual(ns["form_problems"], [
            "Phone is required.", "Tick the box to agree to how this information is used."])

    async def test_refused_form_comes_back_filled_in_without_passwords(self):
        handler = await self.submit(dict(ACCOUNT, **dict(ORGANISATION, phone="nope")))
        form = handler.rendered[-1][1]["form"]
        self.assertEqual(form["username"], "tendai")
        self.assertEqual(form["email"], "tendai@zai.example")
        self.assertEqual(form["org_name"], "Kumusha Labs (Pvt) Ltd")
        self.assertEqual(form["phone"], "nope")
        self.assertNotIn("tendai-password-1", json.dumps(form))

    async def test_mismatched_passwords_create_no_account(self):
        # NativeAuthenticator alone creates the account and then reports the error.
        handler = await self.submit(dict(ACCOUNT, **INDIVIDUAL, signup_password_confirmation="different-pw-123"))
        self.assertIsNone(UserInfo.find(self.db, "tendai"))
        self.assertEqual(handler.rendered[-1][1]["form_problems"], ["The two passwords do not match."])

    async def test_weak_password_stores_nothing_and_keeps_the_answers(self):
        weak = dict(ACCOUNT, signup_password="short", signup_password_confirmation="short")
        handler = await self.submit(dict(weak, **INDIVIDUAL))
        self.assertIsNone(UserInfo.find(self.db, "tendai"))
        self.assertIsNone(self.stored("tendai"))
        self.assertEqual(handler.rendered[-1][1]["form"]["project_name"], "Clinic triage")

    async def test_taken_username_does_not_overwrite_the_first_application(self):
        await self.submit(dict(ACCOUNT, **INDIVIDUAL))
        second = dict(ACCOUNT, email="other@zai.example", **dict(INDIVIDUAL, full_name="Somebody Else"))
        handler = await self.submit(second)
        self.assertEqual(handler.rendered[-1][1]["alert"], "alert-danger")
        self.assertEqual(self.stored("tendai")["who"], "Tendai Moyo")
        self.assertEqual(UserInfo.find(self.db, "tendai").email, "tendai@zai.example")

    async def test_signup_closed_is_still_a_404(self):
        self.auth.enable_signup = False
        with self.assertRaises(web.HTTPError) as raised:
            await self.submit(dict(ACCOUNT, **INDIVIDUAL))
        self.assertEqual(raised.exception.status_code, 404)

    async def test_consent_wording_and_time_are_kept_with_the_answers(self):
        await self.submit(dict(ACCOUNT, **INDIVIDUAL))
        row = self.db.execute(self.ns["_ar_table"].select()).first()
        record = json.loads(row.answers)
        self.assertEqual(record["consent_text"], self.ns["_AR_CONSENT"])
        self.assertTrue(record["consent_at"].endswith("Z"))
        self.assertEqual((row.applicant_type, row.gpu_hours), ("individual", 30))
        self.assertNotIn("tendai-password-1", row.answers)

    async def test_discarding_the_account_removes_the_application(self):
        await self.submit(dict(ACCOUNT, **INDIVIDUAL))
        self.auth.delete_user(types.SimpleNamespace(name="tendai"))
        self.assertIsNone(UserInfo.find(self.db, "tendai"))
        self.assertIsNone(self.stored("tendai"))

    async def test_account_without_an_application_shows_none(self):
        self.auth.create_user("oldtimer", "oldtimer-password", email="old@zai.example")
        self.assertIsNone(self.stored("oldtimer"))


class Pages(Case):
    def render(self, name, **variables):
        env = Environment(autoescape=True, loader=DictLoader({
            "page.html": "{% block script %}{% endblock %}{% block main %}{% endblock %}",
            "zai-panel.html": "",
            name: open(os.path.join(REPO, "templates", name)).read()}))
        return env.get_template(name).render(base_url="/hub/", **variables)

    def test_signup_page_has_every_field_the_server_checks(self):
        page = self.render("signup.html", ask_email=True, **self.page_vars)
        names = ["applicant_type", "sector_other", "consent"] + [
            field[0] for field in self.ns["_AR_EVERYONE"] + self.ns["_AR_ORGANISATION"]]
        for name in names:
            self.assertIn(f'name="{name}"', page, name)
        for option in self.ns["_AR_SECTORS"] + self.ns["_AR_ORG_TYPES"]:
            self.assertIn(f">{option}</option>", page)
        self.assertIn(self.ns["_AR_CONSENT"], page)

    def test_required_is_left_to_the_script_so_no_script_still_works(self):
        # Written into the HTML, "required" on the organisation fields would
        # block an individual whose browser runs no scripts.
        page = self.render("signup.html", ask_email=True, **self.page_vars)
        self.assertNotIn("required data-required", page)
        # Every question plus "which sector" and the consent box.
        marked = re.findall(r"<(?:input|select|textarea)[^>]*data-required", page)
        self.assertEqual(len(marked), len(self.ns["_AR_EVERYONE"] + self.ns["_AR_ORGANISATION"]) + 2)
        self.assertIn("el.required = on", open(os.path.join(REPO, "templates", "signup.html")).read())

    def test_signup_page_refills_a_refused_form_and_lists_the_problems(self):
        page = self.render(
            "signup.html", ask_email=True, alert="alert-danger", result_message="Please correct",
            form=dict(ORGANISATION, username="tendai", gpu_hours=30, problem='<b>"quoted"</b>'),
            form_problems=["Phone is required."], **self.page_vars)
        self.assertIn('value="Kumusha Labs (Pvt) Ltd"', page)
        self.assertIn('value="tendai"', page)
        self.assertIn('value="organisation" required checked', page)
        self.assertIn("<option selected>Startup</option>", page)
        self.assertIn("<li>Phone is required.</li>", page)
        self.assertNotIn("<b>", page)

    def test_signup_page_without_the_feature_is_the_plain_form(self):
        page = self.render("signup.html", ask_email=True)
        self.assertNotIn("applicant_type", page)
        self.assertIn('name="username"', page)

    async def test_approve_page_shows_the_application(self):
        self.auth.create_user("tendai", "tendai-password-1", email="tendai@zai.example")
        hostile = dict(ORGANISATION, problem="<script>alert(1)</script>", gpu_hours=30, duration_months=6)
        self.ns["_ar_save"](self.db, "tendai", hostile)
        self.auth.create_user("oldtimer", "oldtimer-password", email="old@zai.example")
        page = self.render("authorization-area.html", ask_email=True,
                           users=self.db.query(UserInfo).all(), **self.page_vars)
        self.assertIn("Kumusha Labs (Pvt) Ltd · Organisation · 30 GPU hours a month", page)
        self.assertIn("<dt>Registration / incorporation number</dt><dd>12345/2021</dd>", page)
        self.assertIn("No request form on file.", page)
        self.assertNotIn("<script>alert(1)</script>", page)

    def test_approve_page_without_the_feature_is_unchanged(self):
        self.auth.create_user("tendai", "tendai-password-1", email="tendai@zai.example")
        page = self.render("authorization-area.html", ask_email=True,
                           users=self.db.query(UserInfo).all())
        self.assertNotIn("request form", page)
        self.assertNotIn("<details>", page)


if __name__ == "__main__":
    unittest.main()
