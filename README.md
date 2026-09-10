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

## Layout

| Path | What's in it |
|---|---|
| `scripts/` | Numbered, run-in-order deploy scripts — start here |
| `host/` | Host-level units: MIG layout service, cloudflared QUIC tuning |
| `images/` | Dockerfiles for the hub and singleuser images |
| `charts/` | JupyterHub Helm values |
| `k8s/` | cloudflared manifest |
| `docs/BUILD-NOTES.md` | Full build log: what was on the box, what broke, how it was fixed |

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
scripts/99-verify.sh        # health check — run after deploy and after every reboot
```

Order matters: `02-firewall.sh` must run before `03-k3s-gpu.sh`, because k3s binds
its API server to `:6443` on all interfaces.

## Secrets

Nothing sensitive lives in this repo. `TUNNEL_TOKEN` and `CAIMEX_API_KEY` are read
from a gitignored `.env` and injected as Kubernetes secrets at deploy time. See
`.env.example`.

## Prerequisites

Ubuntu 22.04, an NVIDIA driver with MIG-capable hardware (H100/H200/A100),
`nvidia-container-toolkit`, Docker, and a Cloudflare Zero Trust account with a
named tunnel. Versions that this was built and verified against are listed at the
top of `docs/BUILD-NOTES.md`.
