# curly-garbanzo

Reusable build for a **Colab-style, self-service GPU notebook platform**: one NVIDIA
H200 split with MIG into 7 slices, served as JupyterLab to a small team over a
Cloudflare tunnel, with no inbound ports open on the host.

Live reference deployment: `https://notebooks.futurezim.com`

## What you get

- **MIG partitioning** — one H200 (143 GB) → 7 × `1g.18gb` slices, persisted across reboot
- **k3s + nvidia-device-plugin** — each notebook pod claims one MIG slice
- **JupyterHub** (zero-to-jupyterhub) — per-user JupyterLab, password auth, idle culling
- **cloudflared** — in-cluster, 2 replicas, outbound-only; no open firewall ports
- **caimex CLI** preinstalled in every notebook, authenticated for all users, DeepSeek as default model
- **Self-service onboarding** — branded signup, email capture, optional captcha and
  email-link approval, with admin approval as the default gate
- **Spawn profiles** — GPU standard / GPU large / CPU-only, so GPU-less work stops
  consuming one of the seven slices
- **Enforced per-user disk quotas** — XFS project quotas, because local-path
  enforces nothing on its own
- **Multi-node ready** — an opt-in overlay for clusters, plus PyTorchJob/Kueue for
  distributed training that a single notebook pod cannot do ([docs/CLUSTER.md](docs/CLUSTER.md))
- **Colab-style notebook UX** — RAM/CPU meter, live GPU dashboard, Git integration,
  nbgitpuller links, a seeded welcome notebook, and optional object storage
- **Pay-as-you-go GPU service** ([docs/PLATFORM.md](docs/PLATFORM.md)) — prepaid
  credit that admins top up, per-second charging by size, servers stopped when
  credit runs out; sizes and prices in one catalog file
- **Sellable GPU sizes** — re-slice the H200 into mixed MIG layouts (e.g. 71 + 35 +
  18 + 18 GB, or the whole 141 GB) with `scripts/11-gpu-layout.sh`
- **Templates** — PyTorch, ComfyUI and vLLM images, picked at start time
- **Remote access** — SSH / scp / VS Code Remote-SSH and authenticated web ports
  through the existing tunnel, plus the `zai` CLI and a small JSON API
- **Serverless endpoints** — RunPod-compatible `/run` / `/runsync` queue endpoints;
  workers start on demand, bill by the second, and share the GPU with notebooks

## Layout

| Path | What's in it |
|---|---|
| `scripts/` | Numbered, run-in-order deploy scripts — start here |
| `host/` | Host-level units: MIG layout service, cloudflared QUIC tuning |
| `images/` | Dockerfiles for the hub and singleuser images, and `templates/` (ComfyUI, vLLM) |
| `hub-ext/` | The billing/sizes/templates layer and `catalog.yaml` (prices, sizes, templates) |
| `cli/zai` | Customers' command-line tool (start/stop, SSH, balance, serverless jobs) |
| `serverless/` | Serverless gateway (RunPod-compatible queue endpoints) and the worker runtime |
| `charts/` | JupyterHub Helm values — base (single node) plus the multi-node overlay |
| `k8s/` | cloudflared manifest; shared RWX claims and the training-job layer |
| `templates/` | Branding for the hub's login/signup pages (a ConfigMap, not an image rebuild) |
| `docs/BUILD-NOTES.md` | Full build log: what was on the box, what broke, how it was fixed |
| `docs/OPERATIONS.md` | Day-two: onboarding, sizing, quotas, and the two things that take this down |
| `docs/CLUSTER.md` | Running this on more than one node: what breaks, distributed training, notes on serving inference |
| `docs/PLATFORM.md` | Selling GPU time: credit and billing, GPU sizes, templates, SSH/ports/API |
| `docs/SERVERLESS.md` | Serverless endpoints: how they work, using them, data handling; the research behind them |
| `docs/CAIMEX.md` | Optional: a Caimex account for each user at first sign-in, and one-click Open Caimex |

## Deploy

```bash
cp .env.example .env      # fill in TUNNEL_TOKEN, CAIMEX_API_KEY, PUBLIC_HOSTNAME
set -a && . ./.env && set +a

sudo scripts/01-mig.sh     # partition the GPU (reboots may be required)
sudo scripts/02-firewall.sh # close the box BEFORE k3s binds :6443
sudo scripts/03-k3s-gpu.sh  # k3s + device plugin
scripts/04-images.sh        # build images, import into k3s containerd
scripts/05-jupyterhub.sh    # deploy the hub
scripts/06-tunnel.sh        # deploy cloudflared
sudo scripts/07-user-storage.sh          # quota-enforced user storage (restarts k3s)
sudo scripts/08-quota-apply.sh --install-timer
scripts/99-verify.sh        # health check — run after deploy and after every reboot
```

Optional, any time later: `sudo scripts/11-gpu-layout.sh mixed` to sell larger
GPU slices (see [docs/PLATFORM.md](docs/PLATFORM.md#gpu-sizes)), and
`scripts/12-serverless.sh` for serverless endpoints ([docs/SERVERLESS.md](docs/SERVERLESS.md)).

**Claim the `admin` account before the public hostname is live.** NativeAuthenticator
treats a signup named `admin` as pre-authorized, so whoever registers it first
becomes administrator — and JupyterHub showing an `admin` user in `/hub/admin` does
*not* mean it has been claimed. See
[docs/OPERATIONS.md](docs/OPERATIONS.md#the-admin-account-is-load-bearing--and-claimable).

Order matters: `02-firewall.sh` must run before `03-k3s-gpu.sh`, because k3s binds
its API server to `:6443` on all interfaces.

`07` is not optional if users are untrusted: until it runs, a per-user disk
"limit" is a number in a manifest that nothing enforces, and one user can fill the
root filesystem and stop the cluster.

## Secrets

Nothing sensitive lives in this repo. `TUNNEL_TOKEN`, `CAIMEX_API_KEY` and the
optional email / captcha / object-storage credentials are read from a gitignored
`.env` and injected as Kubernetes secrets at deploy time. See `.env.example`.

Every optional integration fails closed: with no `.env` entry, signup approval
stays manual, the captcha is absent, and `spaces` reports object storage as
unconfigured — nothing silently degrades to an insecure default.

## Prerequisites

Ubuntu 22.04, an NVIDIA driver with MIG-capable hardware (H100/H200/A100),
`nvidia-container-toolkit`, Docker, and a Cloudflare Zero Trust account with a
named tunnel. Versions that this was built and verified against are listed at the
top of `docs/BUILD-NOTES.md`.
