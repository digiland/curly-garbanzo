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
