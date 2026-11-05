# Site: cpt-gpu170

HPE Cray XD670 — 8 × H200 SXM (141 GB), 2 × Xeon Platinum 8558 (192 threads),
2 TiB RAM, Ubuntu 24.04. A **single-node kubeadm cluster managed by Rafay**, with
the NVIDIA GPU Operator, Calico, ingress-nginx, Kyverno (Audit mode) and WEKA
already installed.

The repo's numbered scripts assume a bare k3s VM with one GPU. On this host most
of them are harmful, so this directory replaces them.

| Generic script | Here | Why |
|---|---|---|
| `01-mig.sh` | **do not run** | The GPU Operator owns MIG (`nvidia.com/mig.config=h200-35gb-mixed`). `mig-setup.sh` destroys every GPU's instances and would fight the MIG manager on every boot. |
| `02-firewall.sh` | **do not run** | Deny-all inbound on a control-plane node running Calico, WEKA and InfiniBand. |
| `03-k3s-gpu.sh` | **do not run** | Would install a second Kubernetes on ports kubeadm already holds, plus a second device plugin. |
| `04-images.sh` | `build-images.sh` | No Docker and no pushable registry: builds in-cluster with BuildKit and imports into containerd. |
| `05`, `06` | reused via `deploy.sh` | Now honour `KUBECONFIG`, `VALUES_OVERLAY` and `SHARED_HOSTPATH=0`. |
| `07`, `08` | not needed | `wekafs-dir` enforces volume sizes itself (`capacityEnforcement: HARD`). |
| `99-verify.sh` | `verify.sh` | Checks this layout instead of k3s's. |
| `10-cluster-prereqs.sh`, `k8s/training/` | not used | No whole GPUs are advertised while MIG is on. |

## Deploy

```bash
cp .env.example .env            # CAIMEX_API_KEY; TUNNEL_TOKEN + PUBLIC_HOSTNAME if using the tunnel
                                # optional: ADMIN_EMAIL for the admin account
sudo -E sites/cpt-gpu170/build-images.sh    # ~20-40 min the first time
sudo -E sites/cpt-gpu170/deploy.sh          # prints the admin password + 2FA key ONCE
sudo -E sites/cpt-gpu170/verify.sh
sudo    sites/cpt-gpu170/gpu-usage.sh       # any time: who holds which slice
```

Everything runs as root because the only kubeconfig here is
`/etc/kubernetes/admin.conf`.

## What the overlay changes (`values.yaml`)

**GPUs.** The GPU Operator uses the MIG *mixed* strategy, so each slice size is
its own resource and `nvidia.com/gpu` is 0:

| Profile | Resource | Pool | VRAM / SMs | RAM / CPU limit |
|---|---|---|---|---|
| GPU — Standard (default) | `nvidia.com/mig-1g.35gb` | 16 (GPUs 0–3) | 32 GB / 16 | 32 GB / 4 |
| GPU — Large | `nvidia.com/mig-2g.35gb` | 12 (GPUs 4–7) | 32 GB / 32 | 64 GB / 8 |
| CPU only | — | — | — | 16 GB / 4 |

Up to 28 GPU notebooks at once, against 7 on the reference box.

**Runtime.** containerd defaults to `runc` here, and the toolkit honours
`NVIDIA_VISIBLE_DEVICES` from unprivileged pods. The base values put every pod
on the `nvidia` RuntimeClass, which would give CPU-only servers every GPU on the
box. The overlay removes that default; only the GPU profiles set it.

**Storage.** Everything user-facing is on WEKA (`wekafs-dir`):

| Mount | Claim | Size | Access |
|---|---|---|---|
| `~` | `claim-<user>` | 20 Gi each | the user |
| `~/shared` | `jupyter-shared-datasets` | 300 Gi | read-only for users |
| `~/team` | `jupyter-shared-team` | 150 Gi | everyone, read-write |
| `/dev/shm` | memory | 8 Gi | — |

The hub's SQLite database stays on the default class (`openebs-hostpath`, local
NVMe); SQLite is safest on a local disk.

⚠️ **The WEKA filesystem is 1000 GB in total**, so ~25 users fit at 20 Gi alongside
the shared claims. Storage runs out before GPUs do. Grow the WEKA filesystem, or
shrink the shared claims, before onboarding more people.

⚠️ `wekafs-dir` has `reclaimPolicy: Retain`. Deleting a user or a claim does not
free space: remove the `Released` PV by hand. Twenty such PVs from earlier `jup-*`
tests were already present when this was written.

To populate `~/shared`, mount `jupyter-shared-datasets` read-write from an admin
pod, or copy into its directory on the WEKA mount.

## Seeing who is on which GPU

**Admins:** `sudo sites/cpt-gpu170/gpu-usage.sh` prints:

- **Pools:** used, free and total for each MIG profile, across *every*
  namespace, with a `FULL` flag.
- **Slices:** all 28 MIG instances by physical GPU, with VRAM in use, compute
  activity and the pod holding each one. This comes from the GPU Operator's DCGM
  exporter.
- **Holders:** a JupyterHub user name for each slice in use.
- **Waiting:** servers stuck `Pending` for a full pool.

**Users:** the spawn page shows live capacity, e.g. `GPU — Standard — 5 of 16
free`, and marks a pool FULL when it is. Choosing a full pool is refused at once
with a clear message, instead of a 10-minute spinner and a timeout
(`03-gpu-capacity` in `values.yaml`; the hub reads node capacity through
`hub-rbac.yaml`). The spawn page counts only JupyterHub's own servers. If other
namespaces take slices, the scheduler is still the final word, and
`gpu-usage.sh` shows the full picture.

## Usage, budgets and billing

`zai-meter` (`meter/meter.py`, deployed by `meter.yaml`) records who held what,
minute by minute. Users see their own month at **`/hub/usage`** and a "GPU hours
this month" card on the control panel. Admins see every user, the meter's
health, budgets and adjustments, and can download a CSV statement.

**What is counted.** Every 20 s the meter lists running user servers and
records one row per server per minute: user, profile, MIG slice type, CPU and
memory limits, and how busy the slice was (DCGM). Pending servers (queued for a
full pool) are not counted. Hours are the time a server *held* its resources,
busy or not, because nobody else could use them. "GPU busy" and "idle GPU hours"
are reported next to them, to show who holds a slice without using it.

| Line item | Unit |
|---|---|
| `gpu:mig-1g.35gb` (GPU Standard) | slice-hours |
| `gpu:mig-2g.35gb` (GPU Large) | slice-hours |
| `cpu` | core-hours (the server's limit) |
| `mem_gb` | GB-hours (the server's limit) |

**Why it can be trusted.**
- It samples what is running, not start and stop events, so a hub restart, a
  crashed pod or a missed event can't produce a session that never ends. A
  minute is stored once per server (keyed by pod and minute), so repeating a
  sample changes nothing.
- The meter is the only writer. It runs as one replica with the Recreate
  strategy, on SQLite on its own WEKA volume, in rollback-journal mode (WAL is
  unsafe on network filesystems).
- A heartbeat is written for every minute the meter was looking, so gaps are
  visible on the admin page. After a gap, servers that were running across it
  (same pod, started before the gap) are filled in and flagged; a server that
  started and stopped entirely inside a gap is not guessed at.
- Budgets and adjustments are append-only. The latest budget wins, and a
  correction is a new adjustment (negative hours = credit), never an edit.
- A consistent copy is written daily to `/data/backups` and kept for 35 days.
- `meter/test_meter.py` covers counting, re-recording, outages and backfill,
  month boundaries in the configured time zone, pricing, budgets and the API's
  authentication: `cd sites/cpt-gpu170/meter && python3 -m unittest -v test_meter`.
- Only the hub can reach it (NetworkPolicy plus a bearer token in secret
  `zai-meter`), and `verify.sh` checks that it is recording.

**Prices and budgets.** Both are in `meter/config.json`: currency, time zone
(months follow Africa/Harare), rate cards by month (a new card never re-prices
past months), the default GPU budget, and the idle threshold. Redeploy, or
re-apply the `zai-meter-config` ConfigMap; the meter re-reads it within about a
minute, without a restart. With every rate at 0, statements show hours and no
charges. Admins can also set a budget per user, or everyone's default, on
`/hub/usage`; those settings take precedence over the file.

**When a budget runs out,** GPU servers are refused at start with the reason,
and CPU only still works. A running server is not stopped. If the meter is down,
GPU starts are allowed (`METER_FAIL_OPEN`, default true) and the time is filled
in when it returns.

**Getting at the raw data:**
```bash
sudo kubectl -n jupyterhub exec deploy/zai-meter -- python3 -c \
  "import sqlite3; print(sqlite3.connect('/data/meter.sqlite').execute('select count(*) from samples').fetchone())"
```

## Authentication and isolation

### Login

| Control | Setting |
|---|---|
| Signup | Admin approval required (`open_signup: false`); optional reCAPTCHA |
| Passwords | ≥ 12 characters, common passwords rejected |
| Brute force | Account locks for 15 min after 5 failures |
| Two-factor | Optional TOTP. Offered at signup (`allow_2fa`) and via **Two-factor** in the nav (`/hub/2fa`), where users scan a QR into Microsoft Authenticator / any TOTP app or turn it off. **The admin account is created with 2FA on** |
| Admin account | Created by `claim-admin.sh` during deploy, *before* any tunnel exists, with a random password. It is never claimable by a stranger |
| Sessions | Expire after 1 day; cookies marked `Secure` |
| Admin powers | `admin_access: false`: admins manage users and servers but **cannot open a user's server** |

### Between users

| Layer | Isolation |
|---|---|
| GPU | MIG gives each slice its own memory and compute in hardware. CPU-only servers don't use the NVIDIA runtime, so they see no GPU |
| Files | A separate WEKA volume per user, size-capped by WEKA. No user can mount another's |
| Network | Notebooks accept connections only from the hub and proxy, not from each other. Outbound traffic can't reach private ranges, cloud metadata, **or the node itself** (`100.64.0.0/10`, which the chart would otherwise allow) |
| Kubernetes | Notebooks get no service-account token, so they can't call the cluster API |
| Container | Non-root uid 1000, no privilege escalation, all Linux capabilities dropped, default seccomp. The namespace enforces the *baseline* Pod Security Standard |
| Memory | Each server has its own `/dev/shm` and RAM limit |

`verify.sh` checks the Pod Security label, the node egress block,
`admin_access`, and that `admin` exists with 2FA.

### Shared by design (decide whether you want these)

- **`~/team`** is read-write for everyone. Anyone can change or delete anyone
  else's files there. Remove `shared-team` from `values.yaml` if you don't want it.
- **Server sharing:** a user can grant a colleague access to *their own*
  server (`shares!user` in the base values). It is opt-in by the owner, but the
  colleague then has full access to that server and its home directory.
- **The caimex API key** is shared by all users and readable in every server.
  Anyone can copy it, and usage can't be traced to a person.

### Strongly recommended before going public

1. **Cloudflare Access** in front of the hostname (Zero Trust → Access →
   Applications), with your SSO or email one-time codes. Then the login page
   isn't reachable at all by anyone outside your allow-list. This is the
   biggest single improvement, and it is dashboard configuration, not code.
2. Set `RECAPTCHA_KEY` / `RECAPTCHA_SECRET` in `.env` so bots can't fill the
   approval queue.
3. Tell users to enable two-factor. It's opt-in at signup and can also be set
   up later from **Two-factor** in the nav (`/hub/2fa`), which shows a QR to scan
   with Microsoft Authenticator or any TOTP app. NativeAuthenticator still can't
   enforce it, so it stays per-user.

## GPU budgets: warnings and automatic stop

A budget used to be checked only when a GPU server started, so someone with
an hour left could start a server and keep it for weeks. Now a running GPU
server is warned as its owner's monthly hours run down, and stopped after
they run out. The code is the `08-budget-enforcement` block in `values.yaml`.

### Setting a budget

| Where | How |
|---|---|
| **Approve users** | The box beside **Approve** holds the GPU hours per month the person asked for on the request-access form. Change it if you like, then approve: that approves them and sets the budget in one step. Clear the box to approve with no budget of their own |
| **Usage** (`/hub/usage`) | As before: set or change anyone's budget, or the default for everyone |

If the usage meter cannot save the budget, the person is not approved and the
page says so. Approving first would leave them with no limit and nobody told.

People approved before this existed have no budget unless one was set by
hand. They are never warned or stopped.

### What happens to a running GPU server

Every two minutes the hub asks the usage meter how much of the month's budget
each running GPU server's owner has used.

| Usage | What happens |
|---|---|
| Under 90% | Nothing |
| 90% | First warning |
| 100% | Final warning, with the time the server will stop |
| 15 minutes later | The GPU server is stopped. Files are kept. A CPU-only server still starts |

- **Missing data never stops anything.** If the meter cannot be reached, the
  hub does nothing and logs it.
- **Raising the budget cancels the stop**, even inside the 15 minutes.
- **Each warning is given once a month.** What has been decided is kept in the
  hub database (`zai_budget_notices`), so a hub restart forgets nothing.
- **CPU-only servers are never stopped**, however far over budget.
- **A new month starts clean**, because usage does.

### Warn-only mode, and switching enforcement on

It ships in warn-only mode. People are warned, and the hub logs which
servers it would have stopped, but nothing is stopped. In this mode the pages
never promise a stop time.

Watch the log for a while:

```bash
sudo kubectl -n jupyterhub logs deploy/hub | grep "gpu budget"
```

Lines read `gpu budget: warn NAME`, `final NAME`, `would-stop NAME` and
`clear NAME`, each with the hours used and the budget. When `would-stop` is
picking the right servers, set `BUDGET_ENFORCE: "true"` under `hub.extraEnv`
in `values.yaml` and run `deploy.sh`. The log then says `stop NAME`.

| Setting (`hub.extraEnv`) | Default | Meaning |
|---|---|---|
| `BUDGET_ENFORCE` | `"false"` | `"true"` stops servers; anything else is warn-only |
| `BUDGET_GRACE_MINUTES` | `"15"` | Minutes between the final warning and the stop |

At start the hub logs one line saying which mode it is in.

### Where people see the warnings

- The **GPU hours this month** card on the control panel.
- Their own **Usage** page, which shows the stop time during the final warning.

There is no email yet. Someone who stays inside JupyterLab and does not look
at the hub will not see a warning before their server stops. JupyterLab saves
open notebooks every couple of minutes, so files are safe, but a computation
that is still running is lost. Sending the warnings by email needs a sending
address for the site.

### What admins see

On the admin **Usage** page, a marker beside a person's name says `warned`,
`final warning`, `stopped` or, in warn-only mode, `would have stopped`. Hover
for the time.

### Tests

```bash
pip install jupyterhub jupyterhub-nativeauthenticator==1.2.0 pyyaml
cd sites/cpt-gpu170 && python3 -m unittest test_budget_enforcement
```

## The request-access form

Signing up here is an application, not just a username and password. The
applicant says whether they are an individual or an organisation, and the
form asks the questions that fit. An administrator reads the answers on the
**Approve users** page before deciding. The code is the `07-access-requests`
block in `values.yaml`; the form itself is `templates/signup.html`.

### What is asked

| Section | Fields | Asked of |
|---|---|---|
| Account | Username, email, password | Everyone |
| About you | Individual or organisation, full name, phone, country | Everyone |
| Your organisation | Legal or operating name, one-line description, type, registration number, date founded, and a secondary contact's name, email and phone | Organisations |
| Your project | Sector, product or project name, the problem, how it is solved today, how they intend to solve it | Everyone |
| What you need | GPU hours per month, GPU size, months of access | Everyone |
| Consent | Agreement to the information being stored and used to assess the request | Everyone |

For an organisation, the person filling in the form is the primary contact:
their name, phone and account email are the primary contact's details.

### Rules

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
| GPU hours per month | A whole number from 1 to 100,000 |
| Months of access | A whole number from 1 to 60 |
| Sector, organisation type, GPU size | One of the listed options. "Other" sector must be named |

To change a list or the consent wording, edit the top of the
`07-access-requests` block. The form and the checks both read from there.

### What admins see

Under each person on **Approve users** is a line such as "Kumusha Labs,
Organisation, 30 GPU hours a month". Click it to read the whole application.
An account created before this form existed, or by `claim-admin.sh`, shows
"No request form on file".

### What is stored

Applications are kept in the hub database, in the table
`zai_access_requests`: the answers, the consent wording the applicant agreed
to, and when. Passwords are not part of it. **Discard** on the Approve page,
or deleting the account from the admin panel, deletes the application too.

The answers include names and phone numbers, some of them belonging to a
second person. Only administrators can read them.

### Not done yet

- The months of access asked for are recorded but nothing ends access when
  they are up.
- Three colleagues from one organisation each fill in the organisation
  section. There is no "join an existing organisation".

### Tests

```bash
pip install jupyterhub jupyterhub-nativeauthenticator==1.2.0 pyyaml
cd sites/cpt-gpu170 && python3 -m unittest test_access_requests
```

## Caimex accounts and one-click sign-in

Optional. When the five `CAIMEX_` settings are present, a user who signs in
here gets a Caimex account in our organization with the same email and
password, and an **Open Caimex** link in the nav that opens Caimex already
signed in. With any setting missing the feature is off and nothing else
changes. The code is the `06-caimex-accounts` block in `values.yaml`.

### What happens, and when

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

### Setup

1. **Create the service account in Caimex.** The hub signs in as this account
   to send invitations, because Caimex's organization endpoints accept a
   sign-in session and not an API key. Use a dedicated account, not a person's
   and not the organization's owner. Invite it to the organization with the
   **People admin** role, accept the invitation, and verify its email. Leave
   two-factor off on this account: nobody is there to type the code.
2. **Fill in `.env` on the server:**

   | Setting | Value |
   |---|---|
   | `CAIMEX_API_URL` | The Caimex API address. Must be `https` |
   | `CAIMEX_WEB_URL` | The Caimex website address. Must be `https` |
   | `CAIMEX_ORG_ID` | The organization's id: the long value after `/org/` in its Caimex address |
   | `CAIMEX_SERVICE_EMAIL` | The service account's email |
   | `CAIMEX_SERVICE_PASSWORD` | The service account's password |

3. **Run `deploy.sh`.** It stores the five values in the `caimex-provision`
   secret. A redeploy without them keeps the existing secret.
4. **Restart the hub after changing any of them.** It reads them only at
   start: `sudo kubectl -n jupyterhub rollout restart deploy/hub`.

At start the hub logs a line beginning `caimex accounts are OFF` if a setting
is missing or an address is not `https`.

### What admins see

The **Approve users** page has a **Caimex account** column:

| Status | Meaning |
|---|---|
| created at first sign-in | Approved, has not signed in since this was switched on |
| created | The hub registered the account |
| invited | The email already had a Caimex account. It was invited to the organization and its password was left alone |
| failed, retried at next sign-in | Hover for the reason. The hub tries again each time that user signs in |

### When Open Caimex asks for a sign-in

The hub can only hand over a session it fetched while the user typed their
password, and it keeps sessions in memory.

| Situation | What the user sees |
|---|---|
| The hub restarted, or the sign-in is older than 23 hours | A page offering **Sign in again**. After that, Caimex opens signed in |
| Their Caimex password differs from the one here, or they turned on two-factor in Caimex | The Caimex sign-in page. The hub tries their password once, then stops |
| The email already had a Caimex account with a different password | The Caimex sign-in page |

Two-factor on this site does not affect any of this. Two-factor on a user's
Caimex account does: they sign in to Caimex themselves from then on.

### What is stored

| Where | What |
|---|---|
| Hub database, table `zai_caimex_accounts` | Username, email, status, failure reason, and whether to keep trying the hub password |
| Hub memory | The service account's session and one Caimex session per signed-in user. Lost on restart |
| `caimex-provision` secret | The five settings, including the service account's password |
| Nowhere | Users' passwords and Caimex sessions are never written to the database or the log |

### Risks to know about

- **The service account's password is in the hub's secret.** Whoever can read
  it can do whatever that account can do in Caimex. This is why it should be a
  People admin and not the owner.
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

### Troubleshooting

| Symptom | Likely cause |
|---|---|
| No **Open Caimex** link, no **Caimex account** column | A setting is missing or not `https`. Look for `caimex accounts are OFF` in the hub log |
| `failed`: "Caimex service sign-in refused" | Wrong service email or password. The hub waits five minutes before trying again |
| `failed`: "two-factor on" | Turn two-factor off on the service account |
| `failed`: "invitation: HTTP 403" | The service account cannot manage invitations in that organization |
| `failed`: "invitation: HTTP 404" | Wrong `CAIMEX_ORG_ID`, or the service account is not a member |
| `failed`: "HTTP 429" | Caimex's rate limit. It clears by itself |
| Caimex shows the wrong person's name after **Open Caimex** | The browser was already signed in to Caimex as someone else. Reload the Caimex page |

### Tests

`test_caimex_accounts.py` runs the config block against a stand-in Caimex. It
needs the hub's packages:

```bash
pip install jupyterhub jupyterhub-nativeauthenticator==1.2.0 pyyaml
cd sites/cpt-gpu170 && python3 -m unittest test_caimex_accounts
```

## Changing the MIG layout

Do it through the GPU Operator, not `nvidia-smi`. Stop all user servers first,
then change the node's `nvidia.com/mig.config` label (e.g. `all-disabled` for
whole GPUs) and wait for `nvidia.com/mig.config.state=success`. The profiles in
`values.yaml` must then be updated to match the new resource names. Rafay may
manage that label, so coordinate with whoever owns the cluster.

## Verified on the live cluster (24 Sep 2026)

Deployed and working: image build (BuildKit reached quay.io, PyPI and the
caimex installer), the hub behind the Cloudflare tunnel, admin claimed by
`claim-admin.sh`, a GPU notebook spawned with capabilities dropped under the
baseline Pod Security level, and the usage meter recording.

## Not verified yet

- the full-pool refusal message (needs a pool to actually fill)
- a budget refusal, and the meter's GPU-busy figures, with real users
- behaviour past one node (see `docs/CLUSTER.md`)
