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

## Authentication and isolation

### Login

| Control | Setting |
|---|---|
| Signup | Admin approval required (`open_signup: false`); optional reCAPTCHA |
| Passwords | ≥ 12 characters, common passwords rejected |
| Brute force | Account locks for 15 min after 5 failures |
| Two-factor | Offered at signup (`allow_2fa`). **The admin account is created with 2FA on** |
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
3. Tell users to tick "two-factor" at signup. NativeAuthenticator can't
   require it, and can't add it later.

## Changing the MIG layout

Do it through the GPU Operator, not `nvidia-smi`. Stop all user servers first,
then change the node's `nvidia.com/mig.config` label (e.g. `all-disabled` for
whole GPUs) and wait for `nvidia.com/mig.config.state=success`. The profiles in
`values.yaml` must then be updated to match the new resource names. Rafay may
manage that label, so coordinate with whoever owns the cluster.

## Not verified yet

Written and checked against the live cluster's configuration. The chart
renders with this overlay, and `gpu-usage.sh` has run against the live cluster.
**Nothing has been deployed**, so these are still unproven:

- whether the BuildKit pod can reach the caimex installer and PyPI
- the spawn-page capacity hook and the full-pool refusal message
- `claim-admin.sh` writing to a real hub database, and 2FA login with its key
- that notebooks start with all capabilities dropped (the docker-stacks
  start script is not expected to need any)
