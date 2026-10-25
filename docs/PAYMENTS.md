# Paying for credit online

Optional. With Econet's Eco Payments credentials set, the credit form on
**Account** offers **Pay now** beside the requests an administrator approves.
The methods are the ones Econet makes available to ZAI's account; in
October 2026 those are **Bank card** and **EcoCash wallet**. The credit is added as soon as the
payment goes through; nobody has to approve it. Without the credentials the
form is the manual one only and nothing else changes. The code is
`hub-ext/zai_payments.py`, loaded after `zai_credit.py` (`03-zai-platform` in
`charts/jupyterhub-values.yaml`).

The API is Econet's Eco Payments experience API (`eco-payments-xapi` v1,
Anypoint Exchange). It is provider-agnostic: the gateway behind each method
(Sasai for the wallet and card) is chosen by Econet's platform.

## What the user sees

1. On **Account**, under **Request more credit**, they pick an amount and,
   under **Pay now online**, a method. EcoCash also asks for the mobile number
   (+263 and 9 digits).
2. **Pay now** sends them to Sasai's hosted checkout
   (`checkout.sasaipaymentgateway.com`). They enter card details there, or
   approve EcoCash on their phone. ZAI never sees card details.
3. Sasai sends them back to **Account**, which says the payment is waiting to
   be confirmed, with **Continue to the payment page**, **Check payment** and
   **Cancel**. The page checks by itself every 10 seconds for two minutes.
4. When the payment goes through, the credit is added and the page says so.
   A payment that fails or expires says it didn't complete, and they can try
   again.

Cancelling only withdraws the request. A payment already made on the
provider's page is still credited when it goes through: the money arrived.

## What administrators see

Online payments appear under **Credit requests** on **Billing** with their
status and the provider's payment id, and a **Check payment** button. They
have no Approve or Decline: the provider's answer settles them, so a payment
that never arrived can't be credited by mistake. Each credit is a top-up in
the ledger noted "Online payment · <method> · <payment id>", by `eco-payments`.

**Refunds** are a manual **Adjustment** on Billing. The API does not refund
Sasai-backed payments yet (it answers 501).

## How a payment is settled

The hub believes only its own question to the provider,
`GET /payments/{paymentId}`. It asks:

| When | |
|---|---|
| The payer comes back from the payment page | At once |
| Someone presses **Check payment** | At once (at most every 5 seconds per payment) |
| The provider's webhook arrives | At once. The webhook is only a nudge: what it says is never used |
| Always | Every `ECO_PAYMENTS_POLL_SECONDS` (60), for every open payment |

| Answer | What happens |
|---|---|
| `PENDING_AUTHORIZATION` | Nothing yet. Normal until the payer finishes on the payment page |
| `SUCCEEDED` | The credit is added and the request approved, in one step, once. However many checks race, a payment is credited one time |
| `SUCCEEDED`, but for a different amount than asked | Nothing is credited. The payment waits as `AMOUNT_MISMATCH` and the log says so; an administrator settles it by hand |
| `FAILED`, `EXPIRED` | The request is closed and the user told; they can try again |
| No answer, or an error | Nothing changes; the next check tries again |
| Still pending after seven days | The hub stops asking and cancels the request |

## Setup

1. **Get credentials from Econet:** a `client_id` and `client_secret` for
   ZAI's client application in the Anypoint developer portal, subscribed to
   `eco-payments-xapi`.
2. **Fill in `.env` on the server** (see `.env.example`):

   | Setting | Value |
   |---|---|
   | `ECO_PAYMENTS_URL` | `https://api-prod.developer.econet.co.zw/eco-payments/v1` (the default) |
   | `ECO_PAYMENTS_CLIENT_ID` | The client id |
   | `ECO_PAYMENTS_CLIENT_SECRET` | The client secret |
   | `PUBLIC_HOSTNAME` | Already there: the site's public name. The payer comes back to it, and the webhook is registered on it |

   Econet accepts only an `https` return address on ZAI's own site. Ask
   Econet which website is registered for the client id: it must be
   `PUBLIC_HOSTNAME`. Without an `https` address (a laptop at
   `http://127.0.0.1`) the hub leaves the return address out: the payer is
   not sent back, and the payment is still confirmed by the checks below.

3. **Run `scripts/05-jupyterhub.sh`.** It stores them in the `eco-payments`
   secret. A run without the secret keeps the existing one.
4. **Restart the hub after changing them:**
   `kubectl -n jupyterhub rollout restart deploy/hub`.

At start the hub logs `payments: on, …` or `payments are OFF: …` with the
reason. With `PUBLIC_HOSTNAME` set it registers
`https://PUBLIC_HOSTNAME/hub/api/zai/payments/webhook` with the provider once.
Without it, payments still work; they are confirmed by the checks above.

The methods offered come from the provider's `GET /payment-methods`, checked
at start and every hour, so a method Econet turns off disappears from the form.

## What is stored

| Where | What |
|---|---|
| Billing database, table `zai_payments` | Each payment: the credit request it pays, user, amount, method, our reference, the provider's payment id, status, the payment page address, and whether and with which ledger row it was credited. Never deleted |
| Billing database, table `zai_credit_requests` | The request, with method `online` and the payment id as its reference |
| `eco-payments` secret | The address, client id, client secret and public address |
| Nowhere | The client secret is never written to the log or the database |

## Risks to know about

- **The client secret is in the hub's secret.** Whoever can read it can
  create payments as ZAI and read their status.
- **The webhook is open to anyone.** That is why nothing in it is believed:
  at most it makes the hub ask the provider about a payment it already knows.
- **Amounts are checked.** Econet's status answer carries the amount; the
  hub credits only when it matches what was asked, to the cent.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| No **Pay now** on Account | `payments are OFF` in the hub log: a credential is missing, or the address is not https |
| "The payment could not be started" | Look for `payments: create for … answered <code>: <Econet's reason>`. 401 or 403: wrong credentials. 400 `VALIDATION_ERROR returnUrl must be an HTTPS URL on your site`: the site's address is not the one registered with Econet |
| A payment shows `AMOUNT_MISMATCH` on Billing | Econet reported a different amount from the one asked. Check the payment with Econet, then add the right amount as a top-up |
| A payment stays waiting | The payer has not finished on the payment page. Econet's answer then says "No gateway order has been bound to this payment yet". **Check payment** asks again. After seven days the hub gives up |
| Paid, but no credit | Press **Check payment** on Billing. If the provider says `SUCCEEDED`, the credit is added then |

## Tests

`hub-ext/test_zai_payments.py` runs the module against the real billing and
credit code and a stand-in for the provider (no network):

```bash
pip install jupyterhub jupyterhub-nativeauthenticator==1.2.0 jupyterhub-kubespawner pyyaml
cd hub-ext && python3 -m unittest test_zai_payments
```
