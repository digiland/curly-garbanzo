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
sudo -E sites/cpt-gpu170/build-images.sh    # ~20-40 min the first time
sudo -E sites/cpt-gpu170/deploy.sh
# claim `admin` over a port-forward BEFORE any public hostname is live (see top-level README)
sudo -E sites/cpt-gpu170/verify.sh
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

## Changing the MIG layout

Do it through the GPU Operator, not `nvidia-smi`. Stop all user servers first,
then change the node's `nvidia.com/mig.config` label (e.g. `all-disabled` for
whole GPUs) and wait for `nvidia.com/mig.config.state=success`. The profiles in
`values.yaml` must then be updated to match the new resource names. Rafay may
manage that label, so coordinate with whoever owns the cluster.

## Not verified yet

Written and checked against the live cluster's configuration, and the chart
renders with this overlay, but **nothing has been deployed**. In particular,
nobody has yet confirmed that the BuildKit pod can reach the caimex installer
and PyPI from inside the cluster.
