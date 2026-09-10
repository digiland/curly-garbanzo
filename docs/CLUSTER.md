# Running this on a cluster

The base deployment is single-node by design: one H200 cut into 7 MIG slices,
images in that node's containerd, shared folders as hostPaths, homes on k3s
local-path. This document covers what changes when there is more than one node,
and what stops being JupyterHub's job entirely.

Nothing here is applied to the running single-node deploy. The cluster config is
an overlay on top of the base values file, and the base file is unchanged.

---

## 1. What breaks across nodes, and why

Six things. Two fail loudly, which is the good case; the rest fail quietly.

| # | Base config | What happens on a cluster |
|---|---|---|
| 1 | `pullPolicy: Never`, images via `ctr images import` | **Loud.** A pod on any other node never even attempts a pull. `ErrImageNeverPull`. |
| 2 | `~/shared`, `~/team` as `hostPath` | **Silent.** Every node creates its own empty copy. Two users on different nodes see different `~/team` and neither is told. |
| 3 | Homes on `local-path` | **Silent.** local-path is hostPath with `WaitForFirstConsumer`: the PVC binds to whichever node the first spawn landed on and pins the user there forever. If that node's GPUs are full they cannot spawn while the cluster idles. |
| 4 | `runtimeClassName: nvidia` in `extraPodConfig` | **Loud.** Applies to every pod including the CPU profile; a CPU node without nvidia-container-toolkit cannot create it. |
| 5 | `userScheduler.enabled: false` | Users scatter across nodes, so no node is ever free for a multi-GPU job and none can be drained. |
| 6 | `scripts/07`/`08` XFS quotas | Per-node, and superseded: on shared storage the quota is the PVC size and the backend's own limits. |

Plus `.items[0]` in `99-verify.sh` and `03-k3s-gpu.sh`, which reports one node's
GPUs as though they were the cluster's. Fixed — the check now sums across nodes
and adds cluster-only assertions.

**What ports unchanged:** the hub, NativeAuthenticator onboarding, the themed
pages, idle culling, the `loadRoles` fix, cloudflared, and the NVIDIA device
plugin (a DaemonSet already — new GPU nodes are covered automatically).

## 2. Order of work

Storage first: it fixes 2 and 3 together and everything else assumes it.

```
# 1. an RWX storage class must exist first -- see §3
kubectl apply -f k8s/cluster/shared-pvcs.yaml

# 2. images to a registry (mandatory: fixes #1)
REGISTRY=ghcr.io/digiland scripts/04-images.sh

# 3. node labels, Training Operator, Kueue
scripts/10-cluster-prereqs.sh
kubectl apply -f k8s/training/user-rbac.yaml   # lets notebooks submit jobs

# 4. substitute REGISTRY_PLACEHOLDER and RWX_CLASS_PLACEHOLDER, then
helm upgrade -i jupyterhub jupyterhub/jupyterhub -n jupyterhub \
  -f charts/jupyterhub-values.yaml \
  -f charts/jupyterhub-values-cluster.yaml

scripts/99-verify.sh
```

Order matters at step 4: Helm merges maps key-by-key and **replaces lists
wholesale**. The overlay must come second, and it restates `profileList` in full
for that reason.

## 3. Storage

Pick by scale, not by preference — this is the decision that is expensive to
reverse.

| Users / GPUs | Backend | Notes |
|---|---|---|
| < 30 users, notebooks only | NFS (`nfs-subdir-external-provisioner`) | Simplest thing that is correct. Fine for homes and `~/team`. |
| Notebooks + HA | Longhorn RWX, Rook/CephFS | Replicated; survives node loss. |
| Real training at 50+ GPUs | Lustre, GPFS, Weka, VAST | NFS will not feed it. Checkpointing a large model writes hundreds of GB from every rank at once. |
| Any scale, datasets | Object storage, streamed | This is the actual argument for the S3/Spaces glue in `images/singleuser/30-objectstore.sh`. |

## 4. Node pools

`scripts/10-cluster-prereqs.sh` labels each node `caimex.io/pool`:

- `cpu` — no GPU. Data prep, analysis, writing the code you are about to queue.
- `mig` — MIG-partitioned. Interactive and single-GPU work.
- `gpu` — whole GPUs. Training.

**Do not MIG your training nodes.** MIG instances have no NVLink peer-to-peer
path between them, so NCCL falls back through host memory and DDP across 7
slices is slower than one whole GPU. On a large cluster, MIG two or three nodes
for interactive work and leave the rest whole.

`migStrategy=single` (set in `scripts/03-k3s-gpu.sh`) also requires every GPU on
a node to be partitioned identically. Mixing MIG and whole-GPU nodes needs
`migStrategy=mixed`, at which point resources are named `nvidia.com/mig-1g.18gb`
rather than `nvidia.com/gpu` and the profiles must be updated to match.

## 5. Training

**The notebook is the launchpad, not the compute.**

A KubeSpawner pod is one pod. There is no rank-0 election, no headless service
for workers to find each other, no gang scheduling — `torchrun --nnodes=8`
inside a notebook has nothing to talk to. Three tiers:

- **1 GPU** — a spawn profile. Works today.
- **8 GPUs, one node** — a spawn profile (`GPU — 8 x GPU`). DDP/FSDP within one
  host, no operator needed.
- **Multi-node** — a `PyTorchJob` (`k8s/training/pytorchjob-example.yaml`). The
  Training Operator creates the pods and sets `MASTER_ADDR`, `MASTER_PORT`,
  `RANK` and `WORLD_SIZE`. Users submit from a notebook and the job outlives it.

Two things that are not optional at scale:

**Gang scheduling (Kueue).** The failure mode is not a crash, it is a deadlock:
two users each ask for 64 GPUs on a 100-GPU cluster, Kubernetes places 50 pods
each, and both wait on the other's GPUs forever. Every GPU busy, nothing
training. Kueue admits a job only when all of its pods can be placed at once.

**Fabric.** NCCL over plain Ethernet is usually the difference between 32 GPUs
giving you 30x and giving you 10x. On InfiniBand/RoCE: the RDMA device plugin,
`NCCL_IB_HCA` set to your adapters, `NCCL_IB_DISABLE=0`, and hostNetwork on job
pods. This is worth more tuning attention than anything else in this document.

The idle culler (1 h) is safe *because* of the launchpad split: it sees notebook
pods, never PyTorchJob pods. If you ever let users train in-notebook, raise the
timeout or they will lose work at hour one.

Also worth adding before you have many users: **DCGM exporter + Prometheus +
Grafana**. Past a few dozen GPUs, "is anyone actually using these" stops being
answerable by looking, and idle GPUs are the dominant cost.

---

## 6. Notes: serving inference

Not built — notes for the decision, since it is a different shape of problem
from both notebooks and training.

**The mismatch.** A notebook holds a GPU for as long as the tab is open. A
training job holds GPUs and exits. Inference is neither: it is a long-lived
service that must stay warm, answer in milliseconds, and scale on request rate
rather than on a queue. None of JupyterHub, the Training Operator, or Kueue is
built for that.

**Three levels, and they are genuinely different systems:**

1. *In-notebook inference.* Already works — load a model in a GPU profile and
   call it. Fine for evaluating a checkpoint. Not a service: it dies with the
   notebook, the culler kills it at one hour idle, and it serves one user.

2. *A shared internal endpoint.* The common case, and the one most teams
   actually want: one vLLM or TGI deployment per model, behind a Service, that
   every notebook on the cluster calls over HTTP. Continuous batching means one
   node serves many users far better than each user loading their own copy —
   which is the real argument, because today N users experimenting with the same
   7B model means N copies of its weights on N GPUs.

3. *Production serving.* KServe or Ray Serve: autoscaling, canary rollouts,
   scale-to-zero, request metrics. Only worth it when something outside the
   cluster depends on the endpoint.

**Things that decide the design:**

- *MIG is good here, unlike in training.* An 18 GB slice comfortably serves a
  quantised 7–13B model, and inference does not need cross-GPU NCCL. The `mig`
  pool is the right home for small-model endpoints.
- *Scale-to-zero matters more than throughput* for internal endpoints. A model
  nobody queried today should not hold a GPU. KServe and Knative do this;
  a plain Deployment does not.
- *Model weights want to be on shared storage,* not baked into images. A 70B
  checkpoint in a container image makes every rollout a multi-hundred-GB pull.
  Mount them from the same RWX claim, or stream from object storage.
- *Separate the pools.* Serving pods must not compete with notebooks and
  training for GPUs, or a burst of traffic starves the queue and vice versa.
  In practice: a `caimex.io/pool=serve` label and a Kueue quota that excludes it.
- *The user-facing question is authentication.* JupyterHub already knows who
  everyone is; an internal endpoint usually should too, if only to attribute
  cost. Simplest workable version is a token issued per user and checked at the
  gateway.

**Cheapest useful first step:** one vLLM Deployment on a MIG slice, with a
Service, and `OPENAI_BASE_URL` preset in `singleuser.extraEnv` so every notebook
can call it with the OpenAI SDK and no setup. Roughly a day, and it removes the
"everyone loads their own copy of the same model" waste immediately.
