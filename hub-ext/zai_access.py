"""The request-access form.

NativeAuthenticator's signup keeps a username, an email and a password; an
administrator deciding who gets GPU time needs to know who is asking and what
for. This adds an application to every signup, held in zai_access_requests
(hub database) and shown to admins on /hub/authorize. See
docs/ACCESS-REQUESTS.md. Loaded by charts/jupyterhub-values.yaml,
03-zai-platform, after zai_platform (whose catalog lists the sizes).

The applicant first says whether they are an individual or an organisation; an
organisation answers more questions. The browser hides and requires fields to
match (templates/signup.html), but the rules are enforced HERE, because a
browser's rules are only a courtesy: an application that fails them creates no
account.

The form's lists and the consent wording live in this file and reach the
template as `access_form`, so there is one copy of each. Without this module
there is no `access_form` and the signup page is the plain three-field form.
"""
import datetime, json, re

import sqlalchemy as sa
from nativeauthenticator import NativeAuthenticator
from nativeauthenticator.handlers import SignUpHandler
from tornado import web
from traitlets.log import get_logger

_AR_SECTORS = [
    "Health", "Education", "Language / natural language processing",
    "Financial inclusion", "Agriculture", "Climate / energy",
    "Creative economy", "Public sector", "Other"]
_AR_ORG_TYPES = [
    "Startup", "MSME", "NGO / PVO", "Creative studio", "Innovation hub",
    "Academic institute", "Public entity", "Corporate"]
# Filled in by configure() from the platform catalog's sizes, plus "unsure".
# Changed in place, because the field list below holds this very dict.
_AR_GPU_SIZES = {"unsure": "Not sure yet"}
_AR_TYPES = {"individual": "Individual", "organisation": "Organisation"}
_AR_CONSENT = ("I agree that the information on this form may be stored and "
               "used to assess my request and to contact me.")

# (field, label shown to admins, kind, limit). The order is the order
# admins read them in. kind: text / long = free text up to `limit`
# characters; phone; email; date; whole = a whole number from 1 to
# `limit`; a list or dict = one of its entries.
_AR_EVERYONE = [
    ("full_name", "Full name", "text", 120),
    ("phone", "Phone", "phone", None),
    ("country", "Country", "text", 80),
    ("sector", "Sector", _AR_SECTORS, None),
    ("project_name", "Product or project", "text", 120),
    ("problem", "Problem being solved", "long", 1000),
    ("current_solution", "How it is solved today", "long", 1000),
    ("approach", "How they intend to solve it", "long", 1000),
    # An estimate for capacity planning; credit is what limits use (zai_platform).
    ("gpu_hours", "Expected GPU hours per month", "whole", 100000),
    ("gpu_size", "Size they expect to use most", _AR_GPU_SIZES, None),
    ("duration_months", "Months of access needed", "whole", 60)]
_AR_ORGANISATION = [
    ("org_name", "Legal / operating name", "text", 160),
    ("org_description", "Organisation in one line", "text", 200),
    ("org_type", "Organisation type", _AR_ORG_TYPES, None),
    ("org_registration", "Registration / incorporation number", "text", 60),
    ("org_founded", "Founded / incorporated", "date", None),
    ("contact2_name", "Secondary contact: name", "text", 120),
    ("contact2_email", "Secondary contact: email", "email", None),
    ("contact2_phone", "Secondary contact: phone", "phone", None)]
_AR_PHONE = re.compile(r"^\+?[0-9][0-9 ()\-]{6,19}$")
_AR_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s.]{2,}$")


def _ar_check(label, kind, limit, value):
    """The cleaned value, or raise ValueError with what is wrong."""
    if not value:
        raise ValueError(f"{label} is required.")
    if kind in ("text", "long"):
        if len(value) > limit:
            raise ValueError(f"{label} must be {limit} characters or fewer.")
    elif kind == "phone":
        if not _AR_PHONE.match(value):
            raise ValueError(f"{label} must be a phone number, digits with an optional leading +.")
    elif kind == "email":
        if not _AR_EMAIL.match(value) or len(value) > 320:
            raise ValueError(f"{label} must be an email address.")
    elif kind == "date":
        try:
            day = datetime.date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{label} must be a date.") from None
        if day > datetime.date.today():
            raise ValueError(f"{label} cannot be in the future.")
        if day.year < 1800:
            raise ValueError(f"{label} must be a real date.")
        return day.isoformat()
    elif kind == "whole":
        # Plain digits only: str.isdigit() also accepts characters such
        # as "²" that int() then refuses.
        if not re.fullmatch(r"[0-9]{1,9}", value) or not 1 <= int(value) <= limit:
            raise ValueError(f"{label} must be a whole number from 1 to {limit}.")
        return int(value)
    elif value not in kind:
        raise ValueError(f"{label}: choose one of the options.")
    return value


def _ar_validate(get):
    """Check an application. get(name) returns the submitted text.
    Returns (answers that passed, kept for re-showing the form;
    a list of what is wrong, empty when the application is complete)."""
    answers, problems = {}, []
    kind_of_applicant = get("applicant_type")
    if kind_of_applicant not in _AR_TYPES:
        problems.append("Say whether you are applying as an individual or an organisation.")
    else:
        answers["applicant_type"] = kind_of_applicant
    fields = list(_AR_EVERYONE)
    if kind_of_applicant == "organisation":
        fields = _AR_ORGANISATION + fields
    for name, label, kind, limit in fields:
        raw = get(name)
        try:
            answers[name] = _ar_check(label, kind, limit, raw)
        except ValueError as e:
            problems.append(str(e))
            if raw and kind in ("text", "long", "phone", "email", "date"):
                answers[name] = raw[:2000]      # so they can correct it
    if answers.get("sector") == "Other":
        try:
            answers["sector_other"] = _ar_check("Sector (other)", "text", 80, get("sector_other"))
        except ValueError as e:
            problems.append(str(e))
    if get("consent") != "yes":
        problems.append("Tick the box to agree to how this information is used.")
    else:
        answers["consent"] = "yes"
    return answers, problems


def _ar_rows(answers):
    """[(label, value)] for an admin to read, in form order."""
    rows = [("Applying as", _AR_TYPES.get(answers.get("applicant_type"), ""))]
    fields = list(_AR_EVERYONE)
    if answers.get("applicant_type") == "organisation":
        fields = _AR_ORGANISATION + fields
    for name, label, kind, _ in fields:
        value = answers.get(name, "")
        if isinstance(kind, dict):
            value = kind.get(value, value)
        if name == "sector" and value == "Other":
            value = "Other: " + str(answers.get("sector_other", ""))
        rows.append((label, value))
    return rows

#   answers   the validated application as JSON, plus the consent wording
#             the applicant agreed to and when
#   gpu_hours a copy of one answer, as a number, for reports to read
_ar_table = sa.Table(
    "zai_access_requests", sa.MetaData(),
    sa.Column("username", sa.Unicode(255), primary_key=True),
    sa.Column("applicant_type", sa.Unicode(32)),
    sa.Column("gpu_hours", sa.Integer),
    sa.Column("answers", sa.UnicodeText),
    sa.Column("submitted_at", sa.DateTime))
_ar = {"db": None}     # the hub's database session; see _get_handlers_ar

# These share the hub's session, as NativeAuthenticator's own writes do.
# It is rolled back only when one of OUR statements fails, because it is
# unusable until then.


def _ar_save(db, username, answers):
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    record = dict(answers, consent_text=_AR_CONSENT,
                  consent_at=now.isoformat(timespec="seconds") + "Z")
    try:
        db.execute(_ar_table.delete().where(_ar_table.c.username == username))
        db.execute(_ar_table.insert().values(
            username=username, applicant_type=answers["applicant_type"],
            gpu_hours=answers["gpu_hours"], answers=json.dumps(record),
            submitted_at=now))
        db.commit()
    except Exception:
        db.rollback()
        raise


def _ar_load(db, username):
    try:
        row = db.execute(
            sa.select(_ar_table.c.answers, _ar_table.c.submitted_at)
            .where(_ar_table.c.username == username)).first()
    except Exception:
        db.rollback()
        raise
    if row is None:
        return None
    answers = json.loads(row[0])
    org = answers.get("applicant_type") == "organisation"
    return {
        "type": _AR_TYPES.get(answers.get("applicant_type"), ""),
        "who": answers.get("org_name") if org else answers.get("full_name"),
        "gpu_hours": answers.get("gpu_hours"),
        "submitted": row[1].strftime("%d %b %Y") if row[1] else "",
        "rows": _ar_rows(answers)}


def _ar_forget(db, username):
    try:
        db.execute(_ar_table.delete().where(_ar_table.c.username == username))
        db.commit()
    except Exception:
        db.rollback()
        raise


# -- signup ---------------------------------------------------------
_ar_signup_post = None           # the originals, captured by configure()
_ar_signup_render = None


def _signup_render(self, name, *args, **ns):
    # What the applicant typed (never a password), so a refused form
    # comes back filled in. NativeAuthenticator's own refusals -- a
    # taken username, a weak password -- used to come back empty.
    ns.setdefault("form", getattr(self, "_ar_form", {}))
    ns.setdefault("form_problems", getattr(self, "_ar_problems", []))
    return _ar_signup_render(self, name, *args, **ns)


async def _signup_post(self):
    auth = self.authenticator
    if not auth.enable_signup:
        raise web.HTTPError(404)
    answers, problems = _ar_validate(
        lambda name: self.get_body_argument(name, "").strip())
    # NativeAuthenticator creates the account first and compares the two
    # passwords afterwards, so a mistyped confirmation leaves an account
    # behind an error message. Catch it before anything is created.
    if (self.get_body_argument("signup_password", "", strip=False)
            != self.get_body_argument("signup_password_confirmation", "", strip=False)):
        problems.append("The two passwords do not match.")
    self._ar_form = dict(
        answers, username=self.get_body_argument("username", "").strip()[:64],
        email=self.get_body_argument("email", "").strip()[:320])
    if problems:
        self._ar_problems = problems
        html = await self.render_template(
            "signup.html", ask_email=auth.ask_email_on_signup, alert="alert-danger",
            result_message="Please correct the following and send the form again.",
            two_factor_auth=auth.allow_2fa, recaptcha_key=auth.recaptcha_key,
            tos=auth.tos)
        self.finish(html)
        return
    username = auth.normalize_username(self.get_body_argument("username", ""))
    existed = auth.user_exists(username)
    await _ar_signup_post(self)
    # Only for an account this request created: a taken username or a
    # weak password must not overwrite somebody else's application.
    if existed or not auth.user_exists(username):
        return
    try:
        _ar_save(self.db, username, answers)
    except Exception as e:
        get_logger().error("access request for %s not saved: %s",
                           username, type(e).__name__)


# An application goes when its account does: Discard on /hub/authorize,
# or a deletion from the admin panel.


_ar_delete_user = None           # the original, captured by configure()


def _delete_user(self, user):
    result = _ar_delete_user(self, user)
    try:
        _ar_forget(self.db, user.name)
    except Exception as e:
        self.log.error("access request for %s not removed: %s",
                       user.name, type(e).__name__)
    return result


_na_get_handlers_ar = None       # the original, captured by configure()


def _get_handlers_ar(self, app):
    try:
        _ar_table.create(app.db.get_bind(), checkfirst=True)
        _ar["db"] = app.db
    except Exception as e:
        self.log.error("access requests will not be saved, no table: %s", e)
    return _na_get_handlers_ar(self, app)


def _ar_for(username):
    """A user's application for the Approve page, or None."""
    try:
        return _ar_load(_ar["db"], username)
    except Exception:
        return None


def configure(c):
    global _ar_signup_post, _ar_signup_render, _ar_delete_user, _na_get_handlers_ar
    # The sizes a server can be started with, from the platform catalog when
    # it is loaded; the field still works, with just "unsure", without it.
    sizes = {}
    try:
        import zai_platform
        sizes = {s["slug"]: s["name"] for s in zai_platform.CATALOG.sizes}
    except Exception:
        pass
    _AR_GPU_SIZES.clear()
    _AR_GPU_SIZES.update(sizes)
    _AR_GPU_SIZES["unsure"] = "Not sure yet"

    # Wrap once, however many times configure() runs.
    if _ar_signup_post is None:
        _ar_signup_post, _ar_signup_render = SignUpHandler.post, SignUpHandler.render_template
        _ar_delete_user = NativeAuthenticator.delete_user
        _na_get_handlers_ar = NativeAuthenticator.get_handlers
        SignUpHandler.post = _signup_post
        SignUpHandler.render_template = _signup_render
        NativeAuthenticator.delete_user = _delete_user
        NativeAuthenticator.get_handlers = _get_handlers_ar

    # access_request is wrapped because JupyterHub calls any callable in
    # template_vars with the current user and hands the template the result.
    tv = dict(c.JupyterHub.get("template_vars", {}) or {})
    tv["access_form"] = {"sectors": _AR_SECTORS, "org_types": _AR_ORG_TYPES,
                         "gpu_sizes": _AR_GPU_SIZES, "consent": _AR_CONSENT}
    tv["access_request"] = lambda user: _ar_for
    c.JupyterHub.template_vars = tv
