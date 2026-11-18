# The request-access form

Signing up here is an application, not just a username and password. The
applicant says whether they are an individual or an organisation, and the
form asks the questions that fit. An administrator reads the answers on the
**Approve users** page before deciding. The code is `hub-ext/zai_access.py`,
loaded after `zai_platform.py` in `03-zai-platform`; the form itself is
`templates/signup.html`.

## What is asked

| Section | Fields | Asked of |
|---|---|---|
| Account | Username, email, password | Everyone |
| About you | Individual or organisation, full name, phone, country | Everyone |
| Your organisation | Legal or operating name, one-line description, type, registration number, date founded, and a secondary contact's name, email and phone | Organisations |
| Your project | Sector, product or project name, the problem, how it is solved today, how they intend to solve it | Everyone |
| What you expect to use | Rough GPU hours a month, the size they expect to use most, months of access | Everyone |
| Consent | Agreement to the information being stored and used to assess the request | Everyone |

For an organisation, the person filling in the form is the primary contact:
their name, phone and account email are the primary contact's details.

## Rules

Every field shown is required. The browser enforces that as the applicant
types, and the hub checks everything again when the form arrives, so a
tampered page cannot skip a question. An application that fails a rule creates
no account, and the form comes back filled in with each problem listed.

| Field | Rule |
|---|---|
| Short text | A limit per field, from 60 to 200 characters |
| The three project questions | Up to 1,000 characters each |
| Phone numbers | Digits, spaces, brackets and hyphens, with an optional leading `+` |
| Date founded | A real date, not in the future |
| Expected GPU hours a month | A whole number from 1 to 100,000 |
| Months of access | A whole number from 1 to 60 |
| Sector, organisation type, size | One of the listed options. "Other" sector must be named. Sizes are the catalog's (`hub-ext/catalog.yaml`), plus "Not sure yet" |

To change a list or the consent wording, edit the top of
`hub-ext/zai_access.py`. The form and the checks both read from there.

## What admins see

Under each person on **Approve users** is a line such as "Kumusha Labs,
Organisation, about 30 GPU hours a month". Click it to read the whole application.
An account created before this form existed, or by `claim-admin.sh`, shows
"No request form on file".

## Who is told

Once outgoing mail is set up (the same settings as password reset,
`docs/PASSWORD-RESET.md`), two emails go out:

| When | To | What |
|---|---|---|
| Someone signs up | `SIGNUP_NOTIFY_EMAIL`, or every admin with an email address on their account when that is empty | Who is asking, as an individual or an organisation, the project, the sector, the expected GPU hours, and a link to their entry on **Approve users** |
| An admin clicks **Approve** | The person approved | Their account is ready, with a link to sign in |

**Revoke** sends nothing. Neither does a signup that is approved as it is
created (an admin's own account) or one the self-approval email already
covers. Without outgoing mail nothing is sent, the signup page says
"Nothing to do until then" instead of "We'll email you", and admins find new
requests on **Approve users** as before.

Set `SIGNUP_NOTIFY_EMAIL` in `.env` (for example `admin@zai.co.zw`), run
`scripts/05-jupyterhub.sh` and restart the hub. The log says
`signup notices: on, new requests to …` or `signup notices are OFF`. A
message that could not be sent is logged as
`signup notice: email could not be sent`.

## What is stored

Applications are kept in the hub database, in the table
`zai_access_requests`: the answers, the consent wording the applicant agreed
to, and when. Passwords are not part of it. **Discard** on the Approve page,
or deleting the account from the admin panel, deletes the application too.

The answers include names and phone numbers, some of them belonging to a
second person. Only administrators can read them.

## What the GPU answers are for

They are an estimate, to help plan capacity. They do not limit anything:
prepaid credit does that (`docs/PLATFORM.md`). Nothing is credited or set at
approval.

## Not done yet

- Colleagues from one organisation each fill in the organisation section.
  There is no "join an existing organisation".
- Months of access are recorded, but nothing ends access when they are up.

## Tests

```bash
pip install jupyterhub jupyterhub-nativeauthenticator==1.2.0 pyyaml
cd hub-ext && python3 -m unittest test_zai_access
```
