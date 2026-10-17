# Caimex accounts and one-click sign-in

Optional. When the five `CAIMEX_` settings are present, a user who signs in
here gets a Caimex account in our organization with the same email and
password, and an **Open Caimex** link in the nav that opens Caimex already
signed in. With any setting missing the feature is off and nothing else
changes. The code is `hub-ext/zai_caimex.py`, loaded by the hub alongside
`zai_platform.py` and `zai_2fa.py` (`03-zai-platform` in
`charts/jupyterhub-values.yaml`).

## What happens, and when

| Moment | What the hub does |
|---|---|
| Signup, Approve, Revoke, Discard | Nothing on Caimex |
| A user's first sign-in after approval | Invites their email to the organization as the service account, then registers their Caimex account with their email, the password they just typed, and the invitation's token. The account joins the organization as an active member |
| Every sign-in | Signs in to Caimex as the user and keeps the session Caimex returns, in memory, for up to 23 hours |
| **Open Caimex** | Forwards the browser to Caimex with that session |

It runs at sign-in because that is the only moment the hub holds a password:
it stores a hash, and registering a Caimex account needs the real thing. An
unapproved user cannot sign in, so they never get a Caimex account.

All of it runs in the background. A slow or failing Caimex never delays or
blocks a sign-in here.

## Setup

1. **Create the service account in Caimex.** The hub signs in as this account
   to send invitations and to set each member's free allowance, because
   Caimex's organization endpoints accept a sign-in session and not an API
   key. Use a dedicated account, not a person's and not the organization's
   owner, on a real mailbox you control. Invite it to the organization with
   the **Admin** role, accept the invitation, and verify its email. Admin is
   needed because setting a spending cap needs billing rights, which People
   admin does not have. Leave two-factor off on this account: nobody is there
   to type the code.
2. **Fill in `.env` on the server** (see `.env.example`):

   | Setting | Value |
   |---|---|
   | `CAIMEX_API_URL` | The Caimex API address, `https://api.caimex.ai` |
   | `CAIMEX_WEB_URL` | The Caimex website address, `https://caimex.ai` |
   | `CAIMEX_ORG_ID` | The organization's id: the long value after `/org/` in its Caimex address |
   | `CAIMEX_SERVICE_EMAIL` | The service account's email |
   | `CAIMEX_SERVICE_PASSWORD` | The service account's password |

3. **Run `scripts/05-jupyterhub.sh`.** It stores the five values in the
   `caimex-provision` secret. A run without them keeps the existing secret.
4. **Restart the hub after changing any of them.** It reads them only at
   start: `kubectl -n jupyterhub rollout restart deploy/hub`.

At start the hub logs a line beginning `caimex accounts are OFF` if a setting
is missing or an address is not `https`.

## The free allowance

ZAI's wallet pays for each member's first `CAIMEX_FREE_ALLOWANCE_USD` dollars
of Caimex use ($5 as shipped, set in `charts/jupyterhub-values.yaml`), once.

| Who | What happens |
|---|---|
| Anyone who joins the organization, however they join (through ZAI Compute, a Caimex invitation, or an email domain rule) | Within about a minute, a lifetime cap of the allowance, with their spend so far reset to zero. A new ZAI Compute account is capped at once |
| Members who were already there when this was switched on | The same fresh allowance, at the first check |
| Owners and admins | Never capped |
| Someone with a cap already, whoever set it | Left alone. To leave someone effectively uncapped, give them a large cap such as $1,000. Do not clear a cap: an empty one is filled in again at the next check, with $0 for anyone who has already had the allowance |
| Someone who has had the allowance, left, and rejoined | A cap of $0: no second allowance |

The hub checks the member list every minute. If a check fails (Caimex down,
the service account refused), nobody is capped that time and the next check
tries again.

**When the allowance runs out.** Caimex refuses the person's requests in ZAI's
workspaces; it does not move them onto their own money there. To carry on,
they switch to their personal workspace in Caimex, which every account has,
and add their own credit. The sign-in and signup pages tell users this.

**The wallet must have money.** The allowance is a ceiling, not a payment:
ZAI's organization wallet has to be topped up in Caimex to cover what members
use, up to the allowance each.

## What admins see

The **Approve users** page has a **Caimex account** column:

| Status | Meaning |
|---|---|
| created at first sign-in | Approved, has not signed in since this was switched on |
| created | The hub registered the account |
| invited | The email already had a Caimex account. It was invited to the organization and its password was left alone |
| failed, retried at next sign-in | Hover for the reason. The hub tries again each time that user signs in |

## When Open Caimex asks for a sign-in

The hub can only hand over a session it fetched while the user typed their
password, and it keeps sessions in memory.

| Situation | What the user sees |
|---|---|
| The hub restarted, or the sign-in is older than 23 hours | A page offering **Sign in again**. After that, Caimex opens signed in |
| Their Caimex password differs from the one here, or they turned on two-factor in Caimex | The Caimex sign-in page. The hub tries their password once, then stops |
| The email already had a Caimex account with a different password | The Caimex sign-in page |

Two-factor on this site does not affect any of this. Two-factor on a user's
Caimex account does: they sign in to Caimex themselves from then on.

## What is stored

| Where | What |
|---|---|
| Hub database, table `zai_caimex_accounts` | Username, email, status, failure reason, and whether to keep trying the hub password |
| Hub database, table `zai_caimex_allowances` | Who has received the free allowance (by Caimex account), how much and when. Never deleted, so the allowance is given once |
| Hub memory | The service account's session and one Caimex session per signed-in user. Lost on restart |
| `caimex-provision` secret | The five settings, including the service account's password |
| Nowhere | Users' passwords and Caimex sessions are never written to the database or the log |

## Risks to know about

- **The service account's password is in the hub's secret.** Whoever can read
  it can do whatever that account can do in Caimex. As an Admin it can manage
  members, invitations, billing settings and spending caps, and revoke keys.
  It cannot withdraw money, transfer ownership or delete the organization,
  which is why it should be an Admin and not the owner.
- **The hub holds a live Caimex session for each signed-in user.** Someone who
  took over the hub process could act as those users in Caimex until the
  sessions expire.
- **Signup emails are not verified here.** A Caimex account is created under
  whatever address the user typed. Approval is the only check, so look at the
  email before approving.
- **Members of a Caimex organization can see each other's name and email.**
  Every user of this site joins the same organization.
- **Two Caimex emails reach each new user:** "verify your email", and an
  invitation the hub has already accepted for them. The signup page says so.
- **Passwords start the same and then drift.** Changing one does not change
  the other.
- **Caimex limits sign-ins and registrations to 30 per 15 minutes from one
  address.** All of the hub's calls come from one address, so more than about
  30 people signing in within 15 minutes will see some delayed until their
  next sign-in.
- **Open Caimex relies on the landing page Caimex built for Google sign-in.**
  If Caimex changes that page, the link falls back to the Caimex sign-in page.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No **Open Caimex** link, no **Caimex account** column | A setting is missing or not `https`. Look for `caimex accounts are OFF` in the hub log |
| `failed`: "Caimex service sign-in refused" | Wrong service email or password. The hub waits five minutes before trying again |
| `failed`: "two-factor on" | Turn two-factor off on the service account |
| `failed`: "invitation: HTTP 403" | The service account cannot manage invitations in that organization |
| `failed`: "invitation: HTTP 404" | Wrong `CAIMEX_ORG_ID`, or the service account is not a member |
| `failed`: "HTTP 429" | Caimex's rate limit. It clears by itself |
| A member has no cap after a minute or two | Look for `caimex allowance` in the hub log. "could not cap ... HTTP 403" means the service account is not an Admin |
| No `caimex allowance` lines in the hub log at all | `CAIMEX_FREE_ALLOWANCE_USD` is empty or not a valid amount |
| Caimex shows the wrong person's name after **Open Caimex** | The browser was already signed in to Caimex as someone else. Reload the Caimex page |

## Tests

`hub-ext/test_zai_caimex.py` runs the module against a stand-in Caimex. It
needs the hub's packages:

```bash
pip install jupyterhub jupyterhub-nativeauthenticator==1.2.0 pyyaml
cd hub-ext && python3 -m unittest test_zai_caimex
```
