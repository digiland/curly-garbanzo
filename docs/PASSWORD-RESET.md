# Password reset by email

Optional. With an email sender configured, the sign-in page offers
**Forgotten your password? Reset it by email**. The user gets a one-time link,
chooses a new password, and is signed in nowhere until they sign in with it.
Without email the page keeps saying an administrator can reset it, and
administrators still can. The code is `hub-ext/zai_reset.py`, loaded after
`zai_2fa.py` (`03-zai-platform` in `charts/jupyterhub-values.yaml`).

## What the user sees

1. **Sign in** → **Reset it by email**, or **Change password** → **Reset it
   by email**.
2. They enter the email address they signed up with and press **Send reset
   link**. The page always answers "If an account uses this address, we've
   sent it a link", whether or not one does.
3. The email, "Reset your ZAI Compute password", holds a link that works
   **once, for 30 minutes**.
4. The link opens **Choose a new password**: the new password twice (at least
   12 characters, not a common password) and, **if they use two-factor
   sign-in, a current authenticator code**.
5. **Password changed.** The old password stops working, every device that
   was signed in is signed out, and a lockout from failed sign-ins is lifted.
   A second email, "Your ZAI Compute password was changed", tells them, so a
   change they didn't make doesn't go unnoticed.

The reset changes the ZAI Compute password only. The user's Caimex password
stays as it was; if **Open Caimex** asks them to sign in, they use their
Caimex password or Caimex's own reset.

A user who lost their two-factor phone, or no longer has the mailbox, asks an
administrator, as before.

## Rules

| Rule | Value |
|---|---|
| Link lifetime | 30 minutes, one use. A newer link cancels the older one |
| Who gets a link | Approved accounts whose email matches, ignoring capitals and spaces. Applicants waiting for approval get nothing |
| Links per email address | 3 an hour |
| Requests per visitor | 20 an hour |
| Wrong authenticator codes | 5 on one link, then the link stops working |
| New password | The signup rules: at least 12 characters, not a common password |

## Setup

The site sends email either through **Resend** or through any **mailbox over
SMTP** (Gmail, Microsoft 365, a hosting provider's mail). Fill in the
server's `.env` (see `.env.example`):

| Setting | Value |
|---|---|
| `PUBLIC_HOSTNAME` | Already there. Links point to `https://PUBLIC_HOSTNAME/hub/reset/...` |
| `MAIL_FROM` | The address emails come from, e.g. `ZAI Compute <no-reply@example.com>`. Falls back to `APPROVAL_FROM_EMAIL` |
| **Either** `RESEND_API_KEY` | Already used for approval emails. The from-address's domain must be verified in Resend |
| **or** `SMTP_HOST` | The mailbox's outgoing server, e.g. `smtp.gmail.com`, `smtp.office365.com` |
| `SMTP_PORT`, `SMTP_SECURITY` | `587` with `starttls` (most providers), or `465` with `ssl` |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | The mailbox's sign-in. For Gmail and Microsoft 365 this is usually an app password, not the account password |

Then run `scripts/05-jupyterhub.sh`, which stores them in the `outgoing-mail`
secret (a run without `SMTP_HOST` and `MAIL_FROM` keeps the existing one),
and restart the hub. The log says `password reset: on, links to …, sent by …`
or `password reset is OFF: <reason>`.

## Security

- **The page never reveals who has an account.** It says the same thing for
  any address, and email is sent in the background, so the reply takes the
  same time.
- **Links are stored only as a SHA-256 hash.** The link itself exists only in
  the email; a copy of the database cannot be used to reset anyone.
- **Links never reach a log.** The module logs who asked, never the link or a
  password, and JupyterHub's request log shows `/hub/reset/[secret]`.
- **Links are built from `PUBLIC_HOSTNAME`, never from the request.** A forged
  request could otherwise make the email point to another site.
- **Two-factor still counts.** A stolen mailbox alone cannot take over an
  account that has two-factor on.
- **Mail goes out encrypted** (`starttls` or `ssl`). Plain text is allowed
  only to a mail server on the same machine, for testing.

## What is stored

| Where | What |
|---|---|
| Hub database, table `zai_password_resets` | For each link: its hash, the username, when it was made, when it expires, when it was used or cancelled, and wrong authenticator codes. Never deleted |
| `outgoing-mail` secret | `public-url`, `from`, and the SMTP settings |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No "Reset it by email" on the sign-in page | `password reset is OFF` in the hub log, with the reason |
| The user never gets the email | `password reset: email could not be sent` in the log: wrong SMTP settings or password, or the from-address is not allowed to send. Also check spam |
| "This link no longer works" | Older than 30 minutes, already used, replaced by a newer link, or 5 wrong authenticator codes |
| No "link sent" line in the log for a request | No approved account has that address, or a limit was reached |

## Tests

`hub-ext/test_zai_reset.py` runs the module against a real NativeAuthenticator
and zai_2fa, without sending email:

```bash
pip install jupyterhub jupyterhub-nativeauthenticator==1.2.0 pyyaml
cd hub-ext && python3 -m unittest test_zai_reset
```
