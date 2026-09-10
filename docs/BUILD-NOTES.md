# GPU Notebook Platform — Build Notes

**Live at:** https://notebooks.futurezim.com
**Built:** 2026-09-10
**What it is:** A Colab-style, self-service JupyterLab environment for ~7 people,
sharing one NVIDIA H200 via MIG, reached over a Cloudflare tunnel with no inbound
ports open.

---

## 1. The machine

| | |
|---|---|
| GPU | 1× NVIDIA H200, 143 GB, driver 575.57.08, CUDA 12.9, **Pass-Through** (not vGPU) |
| CPU / RAM | Intel Xeon Platinum 8592+, 24 cores / 235 GB |
| Disk | 698 GB total |
| Host | KVM guest, Ubuntu 22.04.5, kernel 5.15, cgroup v2 |
| Public IP | 129.212.180.103 |
| Hostname | `gpu-h200x1-141gb-atl1-1` |

### Software installed during this build
| Component | Version |
|---|---|
| k3s | v1.36.4+k3s1 |
| Helm | v3.22.0 |
| nvidia-device-plugin (Helm) | 0.20.0 |
| JupyterHub (zero-to-jupyterhub) | chart 4.4.2 / app 5.5.2 |
| cloudflared | 2026.9.0 (in-cluster, 2 replicas) |
| caimex CLI | 1.19.0 |

Already present, reused, **not** reinstalled: NVIDIA driver 575.57.08,
nvidia-container-toolkit 1.19.1, Docker 29.1.3.

---

## 2. What was there before

The box was running four model-inference containers on the full GPU:

| Port | Container | GPU mem |
|---|---|---|
| 8003 | `gemma-4-31b` | 88.4 GB |
| 8004 | `whisper` | 3.6 GB |
| 8005 | `bge-reranker-v2-m3` | 14.7 GB |
| 8006 | `mxbai-rerank-base-v2` | 2.2 GB |
| — | `mxbai-rerank-tei` | crash-looping, **22,256 restarts** |

`mxbai-rerank-tei` had been failing once a minute for ~2 weeks:
`The --pooling arg is not set and we could not find a pooling configuration`.
It was a *reranker* being launched as an *embedding* model, and redundant anyway
since port 8006 already served the same model successfully.

All five were stopped and retired (restart policies set to `no`). Their configs
are snapshotted at
`/tmp/claude-0/-root/f4262d05-8ab9-4ecd-8beb-fbce55a98d5f/scratchpad/containers-backup-20260910-0758.json`
— note this is a scratch path and will not survive indefinitely; copy it somewhere
permanent if those containers might ever be wanted back.

Their images still occupy ~115 GB. `docker system prune -a` reclaims it if you are
certain the inference stack is gone for good.

---

## 3. Architecture

```
browser
   │  https
   ▼
Cloudflare edge  ── TLS terminates here
   │  encrypted tunnel (outbound-only, no inbound ports)
   ▼
cloudflared (2 replicas, in-cluster)
   │  http
   ▼
proxy-public  (ClusterIP :80)
   │
   ├── hub ── NativeAuthenticator (signup + login + admin approval)
   │
   └── KubeSpawner ──► one pod per user
                         nvidia.com/gpu: 1   (one MIG slice, 16 GiB)
                         PVC 30 Gi           (local-path)
                         caimex CLI preinstalled
```

### Why not the originally-proposed stack
- **Rafay** is a multi-cluster control plane — irrelevant on a single node. Plain
  k3s instead, nothing lost.
- **NVIDIA GPU Operator** was deliberately *not* installed. It wants to manage the
  driver and container toolkit, both already installed and working on this host,
  and would fight them. The standalone device plugin is the only piece needed.

---

## 4. GPU partitioning (MIG)

Layout: **7 × `1g.18gb`** (profile ID 19) — 16.00 GiB and 16 SMs each.

Full profile table advertised by this card:

| Profile | ID | Max instances | Memory | SMs |
|---|---|---|---|---|
| **1g.18gb** | **19** | **7** | **16.00 GiB** | **16** |
| 1g.35gb | 15 | 4 | 32.50 GiB | 26 |
| 2g.35gb | 14 | 3 | 32.50 GiB | 32 |
| 3g.71gb | 9 | 2 | 69.75 GiB | 60 |

Note the profile *name* says 18gb but usable memory is **16.00 GiB**.

**To change the layout** (e.g. to 4 users at 32.5 GiB): stop all user pods, edit
`PROFILE`/`WANT` in `/usr/local/bin/mig-setup.sh`, run it, then restart the device
plugin so it re-advertises.

### Boot persistence — important
MIG *mode* survives reboot. The 7 instances **do not** — the card comes back as one
undivided device. This is handled by:

- `/usr/local/bin/mig-setup.sh` — idempotent rebuild, waits up to 120 s for the driver
- `/etc/systemd/system/nvidia-mig-layout.service` — oneshot, enabled,
  ordered `After=basic.target nvidia-persistenced.service`,
  `Before=k3s.service containerd.service docker.service`

`k3s.service` genuinely resolves `After=nvidia-mig-layout.service`, so k3s will not
enumerate GPUs before the layout is rebuilt.

⚠️ **This has not been tested under a real reboot** — see §9.

---

## 5. Access and security

- `ufw` **active**. Only `22/tcp` inbound. A stale `8000/tcp` allow rule from the
  previous setup was removed.
- No inbound port for the web UI — cloudflared connects *outbound* only.
- k3s API (`:6443`) is bound on all interfaces but firewalled off. The firewall was
  deliberately enabled **before** k3s was installed, not after.
- Traefik and ServiceLB are disabled in k3s so nothing binds host ports 80/443.

⚠️ Docker bypasses ufw via the `DOCKER-USER` iptables chain. Not an issue now (no
published ports), but relevant if containers with `-p` are ever run again.

### Authentication
JupyterHub **NativeAuthenticator** — self-service signup, admin approval.

- `open_signup: false` — registering grants nothing until an admin approves
- Passwords: minimum 10 characters, common passwords rejected
- Admin: `admin`
- Approve new users at **https://notebooks.futurezim.com/hub/authorize**
- Admin panel at **/hub/admin** — see running servers, stop them, free slices

---

## 6. Per-user resources

| Resource | Per user | × 7 | Box has |
|---|---|---|---|
| GPU | 1 MIG slice, 16 GiB | 7 | 7 |
| CPU | 3 cores (0.5 guaranteed) | 21 | 24 |
| RAM | 28 GB (2 GB guaranteed) | 196 GB | 235 GB |
| Disk | 30 Gi PVC | 210 GB | ~396 GB free |

- **Hard cap of 7 concurrent users.** The 8th queues cleanly with
  `Insufficient nvidia.com/gpu` — it looks like a slow spawn, so warn people.
- **Idle culling after 1 hour** releases the slice. This will kill long unattended
  training runs — raise `cull.timeout` if that becomes a problem.
- Home directories persist across restarts and culling; only in-memory state is lost.
- **No tensor-parallel / NVLink / P2P across slices.** Nobody can exceed 16 GiB.
  Using the whole card again means turning MIG off.

---

## 7. The caimex CLI

Preinstalled for every user, pre-authenticated, defaulting to DeepSeek. A user
opens a terminal and `caimex` just works — no login step.

- Image: `local/pytorch-caimex:cuda12` (pytorch-notebook + caimex 1.19.0)
- Binary at `/opt/caimex/bin`, symlinked into `/usr/local/bin` — deliberately
  **outside `$HOME`**, which is masked by the per-user PVC
- Key in k8s secret `caimex-credentials`, injected as `CAIMEX_API_KEY`. Not baked
  into the image, so it rotates without a rebuild
- Seeding hook: `/usr/local/bin/before-notebook.d/10-caimex.sh`
  - `~/.config/caimex-code/caimex.json` — default model, written **only if absent**
    so a user's own choice survives
  - `~/.local/share/caimex-code/auth.json` — rewritten **every start** so key
    rotation propagates
- Default model: `caimex/Caimex2/deepseek-ai/DeepSeek-V4-Flash`

**To rotate the key:**
```bash
kubectl -n jupyterhub create secret generic caimex-credentials \
  --from-literal=api-key='NEW_KEY' --dry-run=client -o yaml | kubectl apply -f -
# users pick it up on their next server restart
```

⚠️ **One key is shared by all users.** It is readable in every pod's environment and
in each user's `auth.json`. Any user can extract it and use it outside the platform,
and usage is not attributable per person. Accepted deliberately; rotate if that
changes.

---

## 8. Gotchas hit during the build

These cost real time and will bite again on any rebuild or version bump.

1. **Device plugin needs `securityContext.privileged=true` under MIG.** Without it
   the pod crash-loops on
   `error getting parent memory info: Insufficient Permissions` — NVML cannot read
   the parent GPU from behind a MIG child. `SYS_ADMIN` +
   `NVIDIA_MIG_MONITOR_DEVICES=all` are *not* sufficient. The GPU Operator does the
   same thing for MIG.

2. **The node carries a hand-applied label** `nvidia.com/gpu.present=true`. The
   device plugin chart's affinity expects Node Feature Discovery labels; rather than
   run NFD for one known GPU node, the label is set directly. **If the node is ever
   rebuilt, reapply it or the plugin silently will not schedule** — no error, just no
   GPUs advertised.

3. **JupyterHub 5 blocked every login.** `Authenticator.allow_all` now defaults to
   `False`, and an empty `allowed_users` no longer means "everyone". NativeAuthenticator
   verified passwords correctly, then JupyterHub's *authorization* layer refused the
   user with `User 'admin' not allowed.` Fixed with `allow_all: true`, which delegates
   the decision to NativeAuthenticator, where approval is still enforced.

4. **A bare HTTP 403 that was really "your form expired."** JupyterHub 5.5 renders
   error pages via `render_template('error.html', sync=True, ...)`, which routes into
   NativeAuthenticator's `_render` — that has no `sync` parameter and returns a
   coroutine, so the error page raised `TypeError` and Tornado emitted a body-less
   403. Fixed by binding the login handler's `render_template` to
   `BaseHandler.render_template`. The underlying cause was mundane: the xsrf cookie
   has `Max-Age=3600`, so a login page left open over an hour fails.

   → Items 3 and 4 are the same shape: **NativeAuthenticator lags JupyterHub 5.5.**
   If you bump the chart, re-test the login flow rather than assuming it carries over.

5. **`CAIMEX_API_KEY` does not authenticate caimex.** It yields
   `Unauthorized: Invalid authorization header`. The credential must be in
   `auth.json` as `{"caimex":{"type":"api","key":"..."}}`. Provider must be `caimex`
   — `openrouter` is rejected despite the `sk-or-` key prefix.

6. **caimex's config dir is `caimex-code`, not `caimex`.** Found via strace. The
   wrong path fails *silently* and falls back to an arbitrary auto-selected model
   (`ollama/qwen3-embedding`), which then errors confusingly.

7. **`before-notebook.d/*.sh` scripts are SOURCED, not executed.** An `exit` in one
   kills notebook startup. The seed script wraps its work in a function and returns.

8. **caimex's cold model-catalog fetch stalls for minutes.** Pre-warmed into
   `/opt/caimex/cache` at build time and copied into each home at start.

9. **The KVM GPU-reset problem never materialised.** MIG enabled straight to
   `current=Enabled` with no reset and no reboot, contrary to expectation.

10. **QUIC UDP buffers.** cloudflared warned about a 208 KiB receive buffer, which
    throttles throughput and reads as sluggish notebooks. Fixed in
    `/etc/sysctl.d/99-cloudflared-quic.conf` (7.5 MB).

---

## 9. Outstanding

**The reboot test has not been run.** Everything is configured and enabled for boot,
and the MIG rebuild script was proven on demand (layout destroyed → restored 7/7),
but the *ordering* has never been exercised by a real restart.

This matters because the box will reboot eventually — host maintenance, kernel
update, or a crash. If the ordering is wrong it returns with an unpartitioned GPU
and every notebook stuck Pending.

To run it:
```bash
reboot
# then, once back:
nvidia-smi -L | grep -c 1g.18gb                    # expect 7
kubectl get node -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}'   # expect 7
curl -sI https://notebooks.futurezim.com/hub/login # expect 200
```
Best done now, while the system is empty, rather than with seven people's work on it.

Minor, deliberately left alone: the xsrf cookie is not marked `Secure`, because the
hub sees plain HTTP on its last internal hop. Harmless in this topology — Cloudflare
forces HTTPS and the tunnel is encrypted.

---

## 9a. User lifecycle — tested end to end

A real second user (`testuser1`) was run through the whole journey on 2026-09-10
and then removed. Results:

| Step | Result |
|---|---|
| Self-signup at `/hub/signup` | works |
| Login **before** approval | correctly **refused** |
| Admin approves at `/hub/authorize` | works (one click per user) |
| Login after approval | 302 → spawn |
| First spawn | ready in ~12 s |
| caimex seeded automatically | yes — auth + DeepSeek default, no user action |

### Storage / workspaces
- Each user gets their **own PVC**, `claim-<username>`, 30 Gi, `local-path`,
  a distinct volume. `claim-admin` and `claim-testuser1` were separate volumes.
- A user sees **only their own** `/home/jovyan`. `testuser1` could not see admin's
  files — no shared filesystem between users.
- Home is writable by the user (root-owned dir, group `users`, set-gid — writes work
  via the pod's `fsGroup`).
- **Storage survives the pod.** A file written, server stopped, server restarted →
  file still there. Verified literally.
- The PVC also survives idle-culling. Only in-memory state (running kernels,
  unsaved variables) is lost.
- ⚠️ **Deleting a user does not delete their PVC.** `claim-<user>` must be removed
  by hand or it keeps consuming disk:
  `kubectl -n jupyterhub delete pvc claim-<username>`
- ⚠️ `local-path` storage is **node-local**, on this box's disk. There is no
  replication and no backup. If this VM is lost, all user work is lost. Worth
  arranging a backup of `/var/lib/rancher/k3s/storage` if the work matters.

### GPU assignment between users
- Slices are a **fungible pool**, not pinned to people. Kubernetes hands out any
  free `nvidia.com/gpu` and the device plugin maps it to a free MIG instance.
- A user gets a **different slice each time**. `testuser1` held
  `MIG-d9d8e1b6…` before stopping and `MIG-a1e87af0…` after restarting. This is
  fine — the slices are identical (16 GiB, 16 SMs).
- **A slice is released the moment the pod stops** — logout, admin stop, or idle
  culling. Verified: stopping the server dropped user pods 2 → 1 and returned the
  slice to the pool immediately.
- Handover is automatic. There is nothing to reassign manually; the next user to
  spawn takes the freed slice.
- **Nobody can take a second slice.** The limit is 1 GPU per pod, one server per
  user, so no one can monopolise the card.
- When all 7 are busy, the 8th user's pod sits `Pending` with
  `Insufficient nvidia.com/gpu` until someone's server stops. It looks like a slow
  spawn — tell people, or an admin can free one at `/hub/admin`.

---

## 10. Operations quick reference

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
```

| Task | Command |
|---|---|
| Health check | `kubectl -n jupyterhub get pods` |
| GPU slices free | `nvidia-smi -L \| grep -c 1g.18gb` and `kubectl get node -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}'` |
| Who is running | `kubectl -n jupyterhub get pods -l component=singleuser-server` |
| Approve new users | https://notebooks.futurezim.com/hub/authorize |
| Admin panel | https://notebooks.futurezim.com/hub/admin |
| Rebuild MIG layout | `systemctl restart nvidia-mig-layout` |
| Hub logs | `kubectl -n jupyterhub logs -l component=hub --tail=50` |
| Tunnel logs | `kubectl -n jupyterhub logs -l app=cloudflared --tail=30` |
| Reconfigure JupyterHub | edit values, then `helm upgrade jupyterhub jupyterhub/jupyterhub -n jupyterhub --version 4.4.2 -f <values>` |

### Key file locations
| What | Where | Also in |
|---|---|---|
| JupyterHub Helm values | *scratch only* | **Appendix A1** |
| caimex image Dockerfile | *scratch only* | **Appendix A2** |
| caimex seed hook | `/usr/local/bin/before-notebook.d/10-caimex.sh` (inside the image) | **Appendix A3** |
| cloudflared manifest | *scratch only* | **Appendix A4** |
| MIG rebuild script | `/usr/local/bin/mig-setup.sh` | **Appendix A5** |
| MIG systemd unit | `/etc/systemd/system/nvidia-mig-layout.service` | **Appendix A6** |
| QUIC sysctl | `/etc/sysctl.d/99-cloudflared-quic.conf` | **Appendix A7** |
| Rebuild from scratch | — | **Appendix B** |
| Full build log / plan | `/root/.claude/plans/dreamy-questing-forest.md` | — |

> Every configuration file is reproduced verbatim in **Appendix A**, so this
> document alone is sufficient to rebuild the system. The working copies were
> authored under
> `/tmp/claude-0/-root/f4262d05-8ab9-4ecd-8beb-fbce55a98d5f/scratchpad`
> and should be treated as disposable — `/tmp` will not survive a reboot.
>
> Still worth committing this file (and the two Dockerfiles) to a git repo, so
> changes made from here on are tracked rather than remembered.

---

## 11. Verification results

Every check below passed on 2026-09-10.

| Check | Result |
|---|---|
| `nvidia-smi -L` → 7 MIG devices | PASS |
| `nvidia.com/gpu` allocatable = 7 | PASS |
| 7 pods scheduled, 8th queues cleanly | PASS |
| 7 pods → 7 **distinct** MIG UUIDs (isolation) | PASS |
| torch in user pod | PASS — `NVIDIA H200 MIG 1g.18gb`, 16.00 GiB, matmul ok |
| MIG layout rebuild (simulated) | PASS — destroyed → restored 7/7 |
| Public URL, TLS, HTTP/2 | PASS |
| Login → spawn → ready | PASS — ~10 s |
| Wrong password → readable error | PASS |
| Expired form → readable error | PASS |
| PVC bound, 30 Gi | PASS |
| `caimex run` with no flags → DeepSeek | PASS |
| Full signup → approve → login → spawn | PASS |
| Login before approval refused | PASS |
| Per-user PVC isolation | PASS — separate volumes, no cross-visibility |
| Work survives stop/restart | PASS |
| GPU slice released on stop, reused by next spawn | PASS |
| **Reboot recovery** | **NOT TESTED** — see §9 |

---

# Appendix A — Configuration files (verbatim)
Everything needed to rebuild this system. These were authored in a scratch
directory under `/tmp`; the copies below are the durable record.

## A1. JupyterHub Helm values

Applied with the helm command in Appendix B. Chart revision 5.

```yaml
# JupyterHub on single-node k3s, 7 x MIG 1g.18gb slices on one H200.

hub:
  image:
    name: local/k8s-hub-native      # custom: stock hub + jupyterhub-nativeauthenticator
    tag: "4.4.2"                    # MUST match chart version
    pullPolicy: Never               # image lives in k3s containerd, not a registry
  config:
    JupyterHub:
      authenticator_class: nativeauthenticator.NativeAuthenticator
      admin_access: true
    Authenticator:
      # JupyterHub 5 defaults allow_all=False, and an empty allowed_users no
      # longer means "everyone" -> every login is refused with "not allowed".
      # NativeAuthenticator does its own gating (is_authorized / admin approval),
      # so delegate to it. Approval is still required; this is not open access.
      allow_all: true
      admin_users:
        - admin
    NativeAuthenticator:
      open_signup: false            # admin approves each signup at /hub/authorize
      minimum_password_length: 10
      check_common_password: true
  extraConfig:
    # JupyterHub 5.5's LoginHandler.render_template intercepts error.html on POST
    # and routes it into _render(**ns). NativeAuthenticator overrides _render(), which
    # (a) has no `sync` parameter and (b) returns a coroutine -- so synchronous error
    # rendering raises and the user gets a bare, body-less HTTP 403 instead of
    # "Login form invalid or expired. Try again."  Bypass the interception: render
    # error pages through the base implementation, which honours sync=True.
    01-nativeauth-render-fix: |
      from jupyterhub.handlers.base import BaseHandler as _JHBase
      from nativeauthenticator.handlers import LoginHandler as _NALoginHandler
      _NALoginHandler.render_template = _JHBase.render_template
    00-nativeauth: |
      import os, nativeauthenticator
      c.JupyterHub.template_paths = [
          f"{os.path.dirname(nativeauthenticator.__file__)}/templates/"
      ]

proxy:
  service:
    type: ClusterIP                 # servicelb is disabled; cloudflared reaches this in-cluster

singleuser:
  image:
    name: local/pytorch-caimex       # pytorch-notebook + caimex CLI preinstalled
    tag: cuda12
    pullPolicy: Never                # local image in k3s containerd, not a registry
  extraEnv:
    # Shared caimex credential. Read at start by
    # /usr/local/bin/before-notebook.d/10-caimex.sh, which writes it to
    # ~/.local/share/caimex-code/auth.json (the env var alone is NOT accepted).
    CAIMEX_API_KEY:
      valueFrom:
        secretKeyRef:
          name: caimex-credentials
          key: api-key
    CAIMEX_MODEL: "caimex/Caimex2/deepseek-ai/DeepSeek-V4-Flash"
  extraPodConfig:
    runtimeClassName: nvidia        # required so the MIG device is injected
  extraResource:
    limits:
      nvidia.com/gpu: "1"           # one MIG slice; migStrategy=single makes this uniform
  cpu:
    limit: 3
    guarantee: 0.5
  memory:
    limit: 28G
    guarantee: 2G
  storage:
    type: dynamic
    capacity: 30Gi
    dynamic:
      storageClass: local-path      # k3s built-in provisioner
  startTimeout: 600
  defaultUrl: /lab

# Idle culling is mandatory: 7 slices total, an idle notebook holds one forever.
cull:
  enabled: true
  users: true
  timeout: 3600                     # 1h idle -> shut down, releasing the GPU
  every: 300
  maxAge: 0

scheduling:
  userScheduler:
    enabled: false                  # single node; nothing to spread across
```

## A2. caimex singleuser image — Dockerfile

Built as `local/pytorch-caimex:cuda12`, then imported into k3s containerd.

```dockerfile
FROM quay.io/jupyter/pytorch-notebook:cuda12-latest
USER root

# Install caimex system-wide (outside $HOME, which is masked by the per-user PVC).
RUN curl -fsSL https://caimex.econetai.co.zw/install.sh -o /tmp/install.sh \
 && CAIMEXCODE_INSTALL_DIR=/opt/caimex/bin bash /tmp/install.sh \
 && rm -f /tmp/install.sh \
 && ln -sf /opt/caimex/bin/caimex /usr/local/bin/caimex \
 && ln -sf /opt/caimex/bin/caimex /usr/local/bin/caimexcode \
 && chmod -R a+rX /opt/caimex

# Pre-warm the model catalog into an image-level cache so a user's first run is
# fast. Runs as root and leaves root-owned droppings under /home/jovyan, which
# would make the seed script unwritable -- clean them up in the same layer.
RUN mkdir -p /opt/caimex/cache /tmp/warm \
 && (HOME=/tmp/warm XDG_CONFIG_HOME=/tmp/warm/.config XDG_CACHE_HOME=/tmp/warm/.cache \
       timeout 180 /opt/caimex/bin/caimex models >/dev/null 2>&1 || true) \
 && cp /tmp/warm/.cache/caimex-code/*.json /opt/caimex/cache/ 2>/dev/null || true; \
    rm -rf /tmp/warm /home/jovyan/.config/caimex-code /home/jovyan/.cache/caimex-code \
           /home/jovyan/.local/share/caimex-code; \
    chown -R "${NB_UID}:${NB_GID}" /home/jovyan 2>/dev/null || true; \
    chmod -R a+rX /opt/caimex/cache; \
    echo "catalog cache:"; ls -la /opt/caimex/cache/

ENV PATH=/opt/caimex/bin:$PATH

COPY 10-caimex.sh /usr/local/bin/before-notebook.d/10-caimex.sh
RUN chmod 0755 /usr/local/bin/before-notebook.d/10-caimex.sh

USER ${NB_UID}
```

## A3. caimex seed hook — 10-caimex.sh

Copied into the image at `/usr/local/bin/before-notebook.d/10-caimex.sh`. Sourced by start.sh, so it must never call `exit`.

```bash
#!/bin/bash
# Seed caimex config + shared credential into the user's home.
#
# $HOME is a per-user PVC mounted over the image, so anything baked under $HOME
# at build time is masked -- these files must be written at container start.
#
# start.sh SOURCES this file, so it must never call `exit` (that would kill the
# notebook startup) and must not leak shell options into the parent shell.
_caimex_seed() {
    local cfg="$HOME/.config/caimex-code"
    local auth="$HOME/.local/share/caimex-code"
    local cache="$HOME/.cache/caimex-code"

    mkdir -p "$cfg" "$auth" "$cache" 2>/dev/null || return 0
    [ -w "$cfg" ] || return 0

    # Default model. Written only if absent, so a user who switches models keeps it.
    if [ ! -f "$cfg/caimex.json" ]; then
        cat > "$cfg/caimex.json" <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
  "model": "${CAIMEX_MODEL:-caimex/Caimex2/deepseek-ai/DeepSeek-V4-Flash}"
}
EOF
    fi

    # Shared credential, refreshed each start so key rotation propagates without
    # rebuilding the image. The env var alone does NOT authenticate -- caimex
    # rejects CAIMEX_API_KEY with "Invalid authorization header"; the credential
    # must be in auth.json under the "caimex" provider.
    if [ -n "${CAIMEX_API_KEY:-}" ] && [ -w "$auth" ]; then
        printf '{"caimex":{"type":"api","key":"%s"}}\n' "$CAIMEX_API_KEY" > "$auth/auth.json"
        chmod 600 "$auth/auth.json" 2>/dev/null
    fi

    # Seed the model catalog; an unseeded first run stalls for minutes fetching it.
    local f
    for f in model-catalog.json models.json; do
        [ -f "$cache/$f" ] || cp "/opt/caimex/cache/$f" "$cache/$f" 2>/dev/null
    done
    return 0
}
_caimex_seed || true
unset -f _caimex_seed
```

## A4. cloudflared deployment

Token supplied separately via the `cloudflared-token` secret.

```yaml
# cloudflared -> JupyterHub. Outbound-only: no inbound port, ufw stays closed.
# Token supplied via secret `cloudflared-token` (created separately, not in git).
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cloudflared
  namespace: jupyterhub
spec:
  replicas: 2                      # 2 replicas = no dropped kernels during restarts
  selector:
    matchLabels: {app: cloudflared}
  template:
    metadata:
      labels: {app: cloudflared}
    spec:
      containers:
      - name: cloudflared
        image: cloudflare/cloudflared:latest
        args:
          - tunnel
          - --no-autoupdate
          - --loglevel
          - info
          - --metrics
          - 0.0.0.0:2000
          - run
        env:
          - name: TUNNEL_TOKEN
            valueFrom:
              secretKeyRef:
                name: cloudflared-token
                key: token
        livenessProbe:
          httpGet: {path: /ready, port: 2000}
          initialDelaySeconds: 10
          periodSeconds: 10
        resources:
          requests: {cpu: 50m, memory: 64Mi}
          limits:   {cpu: 500m, memory: 256Mi}
```

## A5. MIG rebuild script

Lives at `/usr/local/bin/mig-setup.sh`. Idempotent.

```bash
#!/usr/bin/env bash
# Recreate the 7 x 1g.18gb MIG layout. Idempotent: safe to run repeatedly.
# MIG *mode* survives reboot; the GPU/compute instance layout does not.
set -uo pipefail

PROFILE=19          # MIG 1g.18gb
WANT=7

command -v nvidia-smi >/dev/null || { echo "nvidia-smi absent"; exit 1; }

# At boot this may run before the driver/device nodes are ready. Wait, don't fail.
for i in $(seq 1 60); do
    nvidia-smi -L >/dev/null 2>&1 && break
    [ "$i" -eq 60 ] && { echo "driver not ready after 120s"; exit 1; }
    sleep 2
done

# Ensure MIG mode is on (persists across reboot, but re-assert defensively).
if ! nvidia-smi -i 0 --query-gpu=mig.mode.current --format=csv,noheader | grep -q Enabled; then
    echo "MIG mode off; enabling"
    nvidia-smi -i 0 -mig 1 || exit 1
fi

# Already in the desired shape? Do nothing.
if [ "$(nvidia-smi -L 2>/dev/null | grep -c 'MIG 1g.18gb')" -eq "$WANT" ]; then
    echo "MIG layout already correct ($WANT x 1g.18gb)"
    exit 0
fi

echo "Rebuilding MIG layout -> $WANT x 1g.18gb"
nvidia-smi mig -dci  || true      # destroy compute instances first
nvidia-smi mig -dgi  || true      # then GPU instances

LIST=$(printf "%s," $(yes "$PROFILE" | head -n "$WANT") | sed 's/,$//')
nvidia-smi mig -cgi "$LIST" -C || { echo "MIG creation FAILED"; exit 1; }

COUNT=$(nvidia-smi -L | grep -c 'MIG 1g.18gb')
[ "$COUNT" -eq "$WANT" ] || { echo "expected $WANT instances, got $COUNT"; exit 1; }
echo "MIG layout OK: $COUNT x 1g.18gb"
```

## A6. MIG systemd unit

Lives at `/etc/systemd/system/nvidia-mig-layout.service`. Enabled.

```ini
[Unit]
Description=Recreate NVIDIA MIG layout (7 x 1g.18gb)
Documentation=file:///usr/local/bin/mig-setup.sh
# Normal dependencies: guarantees sysinit/udev have run and /dev/nvidia* exist.
After=basic.target nvidia-persistenced.service
Wants=nvidia-persistenced.service
# k3s must not enumerate GPUs before the layout is rebuilt.
Before=k3s.service containerd.service docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/mig-setup.sh
StandardOutput=journal
StandardError=journal
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
```

## A7. QUIC sysctl tuning

Lives at `/etc/sysctl.d/99-cloudflared-quic.conf`.

```ini
# QUIC (cloudflared) wants larger UDP buffers; default 208KiB throttles throughput.
net.core.rmem_max = 7500000
net.core.wmem_max = 7500000
```

---

# Appendix B — Rebuilding from scratch

Exact commands, in order. Assumes the NVIDIA driver and nvidia-container-toolkit
are already installed on the host (they were).

## B1. MIG
```bash
nvidia-smi -pm 0
nvidia-smi -i 0 -mig 1
# A GPU reset was NOT required on this KVM guest; if it is, reboot the VM.
nvidia-smi mig -cgi 19,19,19,19,19,19,19 -C   # 19 = 1g.18gb
nvidia-smi -pm 1
nvidia-smi -L                                  # expect 7 devices
# then install mig-setup.sh + nvidia-mig-layout.service (Appendix A5/A6)
systemctl daemon-reload && systemctl enable --now nvidia-mig-layout.service
```

## B2. Firewall — BEFORE k3s, not after
```bash
ufw allow 22/tcp comment 'ssh'
ufw default deny incoming
ufw default allow outgoing
ufw --force enable
```

## B3. k3s + GPU scheduling
```bash
curl -sfL https://get.k3s.io | \
  INSTALL_K3S_EXEC="--disable traefik --disable servicelb --write-kubeconfig-mode 644" sh -
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# k3s auto-detects nvidia-container-runtime and creates the `nvidia` RuntimeClass.

# REQUIRED: the chart's affinity wants Node Feature Discovery labels.
kubectl label node "$(hostname)" nvidia.com/gpu.present=true --overwrite

helm repo add nvdp https://nvidia.github.io/k8s-device-plugin && helm repo update nvdp
helm upgrade -i nvdp nvdp/nvidia-device-plugin \
  --namespace nvidia-device-plugin --create-namespace --version 0.20.0 \
  --set migStrategy=single \
  --set runtimeClassName=nvidia \
  --set securityContext.privileged=true   # REQUIRED under MIG (see gotcha 1)

# Gate: do not proceed until this prints 7
kubectl get node -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}'
```

## B4. Images
```bash
# Hub image: stock hub + NativeAuthenticator. Tag MUST match the chart version.
cat > Dockerfile.hub <<'EOF'
FROM quay.io/jupyterhub/k8s-hub:4.4.2
USER root
RUN pip install --no-cache-dir jupyterhub-nativeauthenticator==1.2.0
USER 1000
EOF
docker build -t local/k8s-hub-native:4.4.2 -f Dockerfile.hub .
docker save local/k8s-hub-native:4.4.2 | k3s ctr images import -

# Singleuser image with caimex (Appendix A2/A3)
docker build -t local/pytorch-caimex:cuda12 ./caimex-image
docker save local/pytorch-caimex:cuda12 | k3s ctr images import -
```

## B5. JupyterHub
```bash
kubectl -n jupyterhub create secret generic caimex-credentials \
  --from-literal=api-key='<CAIMEX_API_KEY>'

helm repo add jupyterhub https://hub.jupyter.org/helm-chart/ && helm repo update jupyterhub
helm upgrade -i jupyterhub jupyterhub/jupyterhub \
  --namespace jupyterhub --create-namespace --version 4.4.2 \
  -f jupyterhub-values.yaml --wait --timeout 10m
```

## B6. Cloudflare tunnel
```bash
kubectl -n jupyterhub create secret generic cloudflared-token \
  --from-literal=token='<TUNNEL_TOKEN>'
kubectl apply -f cloudflared.yaml

sysctl -p /etc/sysctl.d/99-cloudflared-quic.conf
```

Then in the Cloudflare dashboard: **Zero Trust → Networks → Tunnels → your tunnel
→ Published application**:

| Field | Value |
|---|---|
| Subdomain / Domain | e.g. `notebooks` / `futurezim.com` |
| Type | `HTTP` (**not** HTTPS — Cloudflare terminates TLS at the edge) |
| URL | `http://proxy-public.jupyterhub.svc.cluster.local:80` |

⚠️ Claim the `admin` account over a port-forward **before** mapping the hostname —
NativeAuthenticator auto-approves a user named `admin`, so whoever registers first
becomes administrator.

```bash
kubectl -n jupyterhub port-forward svc/proxy-public 8080:http
# from your laptop: ssh -L 8080:localhost:8080 root@<host>
# browse http://localhost:8080/hub/signup and register as: admin
```
