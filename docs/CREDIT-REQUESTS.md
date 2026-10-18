# Credit requests

Users ask for more credit on their **Account** page. An administrator sends
them payment details, and once the money has arrived approves the request on
the **Billing** page, which adds the credit. Nothing is paid through the site:
payment happens outside it, as before. The code is `hub-ext/zai_credit.py`,
loaded by the hub after `zai_platform.py` (`03-zai-platform` in
`charts/jupyterhub-values.yaml`).

## What the user does

On **Account**, under **Request more credit**, they choose:

| Field | Rules |
|---|---|
| Amount | $10, $25, $50 or $100, or any amount from $5 to $1,000, in dollars and cents |
| How they'll pay | EcoCash, bank transfer, or invoice (for organisations) |
| Payment reference | Optional, up to 100 characters, for someone who has already paid |
| Note | Optional, up to 500 characters |

Under the amount the form shows what it buys at today's prices, for example
"$25.00 is about 18.9 hours on GPU · 18 GB, or 13.4 hours on GPU · 18 GB, high
memory. Includes $3.35 VAT." The VAT rate is `currency.vat_percent` in the
catalog.

After sending, the page says the request is waiting and that an administrator
will email payment details to their sign-up address. A user has one waiting
request at a time, and can cancel it to ask for a different amount. Once it is
decided, the page shows the outcome: the amount added, or the reason it was
declined.

The low-balance warning on Account, the "request credit" link on the start
page, and the out-of-credit messages all point here.

Administrators whose servers are free (`billing.admins_free`) are not offered
the form.

## What the administrator does

The nav shows **Billing (2)** when two requests are waiting. On **Billing**,
**Credit requests** lists each one with the user's email, the amount, how
they will pay, their reference and note.

1. Email the user payment details.
2. When the money has arrived, enter the amount to add (it starts at the
   amount asked; change it if they paid a different amount) and the payment
   reference, then **Approve**. The credit is added as a top-up, with the note
   "Credit request #12 · EcoCash ref ...", and appears in the ledger like any
   other.
3. Or **Decline** with a reason. The user sees the reason.

Below the waiting requests are the last ten decisions.

The **Add credit** form on the same page still works as before, for payments
that did not come through a request.

## Rules the hub enforces

- Only the user can ask or cancel for themselves; only administrators can
  approve or decline.
- One waiting request per user, enforced by the database.
- A request is approved at most once. Approving marks it and writes the
  ledger row in one step, so two administrators pressing Approve together
  add the credit once.
- A request for a user who has since been deleted cannot be approved; decline
  it.
- Approving adds credit only. Nothing changes when a request is sent.

## What is stored

Table `zai_credit_requests` in the billing database
(`/srv/jupyterhub/usage.sqlite`, next to the ledger): username, email at the
time of asking, amount asked, payment method, reference, note, status
(pending, approved, declined or cancelled), when and by whom it was decided,
the amount added, the decline reason, and the ledger row it created. Rows are
never deleted.

## Tests

`hub-ext/test_zai_credit.py` runs the module against the real billing code
and a throwaway ledger. It needs the hub's packages:

```bash
pip install jupyterhub jupyterhub-nativeauthenticator==1.2.0 jupyterhub-kubespawner pyyaml
cd hub-ext && python3 -m unittest test_zai_credit
```
