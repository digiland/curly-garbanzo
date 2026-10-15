# Selling GPU time

How the hub works as a pay-as-you-go GPU service: customers buy credit, pick a
size and a template, and are charged by the second while their server runs.
They can drive it from the browser, the `zai` command-line tool, SSH, or the
API.

| Piece | Where |
|---|---|
| Sizes, prices, templates, billing switches | `hub-ext/catalog.yaml` |
| Billing, capacity, pages, API | `hub-ext/zai_platform.py` (loaded by extraConfig `03-zai-platform`) |
| Pages | `templates/account.html`, `billing.html`, the size picker in `spawn.html` |
| GPU layout | `host/mig-setup.sh`, `scripts/11-gpu-layout.sh` |
| Template images | `images/templates/<name>/`, built by `scripts/04-images.sh` |
| SSH and port proxy | `images/singleuser/` (`zai-sshd`, jupyter-server-proxy) |
| Command-line tool | `cli/zai`, served at `/hub/zai/cli` |

Applying any change to the catalog or module is `scripts/05-jupyterhub.sh`.
It restarts the hub (about 30 s with no logins or starts); running servers
keep running.

## Credit and billing

**Money model.** Every account has a prepaid balance. An admin adds credit
after the customer pays (cash, EcoCash, bank transfer). A running server is
charged its size's `price_per_hour`, to the second, from when it is ready
until it stops: the user stops it, it idles out after an hour, or credit runs
out. Stopped servers and stored files cost nothing.

Money is stored in millionths of the currency unit, so small rates and short
sessions are charged exactly. Each session records its price when it starts,
so a price change applies to new sessions only.

**Admin: `/hub/billing`.** This page shows balances, spend and top-ups this
month, and the full ledger (with CSV export). It also has the form for adding
credit:

- **Top-up:** money received; must be positive.
- **Adjustment:** refunds and corrections; may be negative.

Every row records who made it and a note. Use the note for the payment
reference.

**Customer: `/hub/account`.** This page shows the balance, the price list with
free GPUs, the history, and how to connect from outside. The nav bar shows the
balance on every page and turns amber when it falls below
`billing.low_balance`.

**Enforcement.** `billing.enforce` in the catalog has two states:

| `enforce` | Behaviour |
|---|---|
| `false` (shipped) | Charges are recorded and balances shown. Nobody is refused or stopped. |
| `true` | A start needs credit for `min_minutes_to_start`. When a balance reaches zero, that user's server is stopped within about 30 s (the hub's poll interval). |

With `true`, the out-of-credit stop deletes the pod before telling the hub the
server is gone. JupyterHub only calls `spawner.stop()` for a server that poll
still reports as running. Reporting "stopped" alone would leave the pod, and
its GPU, running unbilled while the hub showed it as gone.

**Going live without locking anyone out:**

1. Deploy with `enforce: false`. Sessions already running when the hub
   restarts were started before billing. They are never charged and never
   stopped by billing.
2. Top up every existing account at `/hub/billing`, or set `signup_credit`.
   It is granted once to each account the first time billing sees it,
   existing accounts included.
3. Set `enforce: true` and run `scripts/05-jupyterhub.sh`.

Admins' own servers are free while `admins_free: true`.

**API for a payment provider.** `POST /hub/api/zai/credits` with an admin
token holding the `admin:users` scope:

```bash
curl -X POST https://<host>/hub/api/zai/credits \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"user": "alice", "amount": "20.00", "kind": "topup", "note": "Paynow 88231"}'
```

That is the hook for automating top-ups: a small webhook receiver for
Paynow, Stripe or another provider verifies the payment and posts here.
There is no online card payment yet.

## GPU sizes

The GPU is cut into MIG slices once. Customers can only rent slices that
exist, so the layout sets what you can sell:

```bash
sudo scripts/11-gpu-layout.sh --dry-run mixed   # does it fit? who would be killed?
sudo scripts/11-gpu-layout.sh mixed
```

| Preset | Slices | Good for |
|---|---|---|
| `team` | 7 × 18 GB | Many small users (the original layout) |
| `mixed` | 71 + 35 + 18 + 18 GB | One large model, one mid-size, two small |
| `large` | 2 × 71 GB | 30–70B models in 4–8 bit |
| `whole` | 1 × 141 GB | The full H200 |

Any layout can also be given directly, e.g. `"3g.71gb 1g.18gb*4"`. The script
works out where each slice must start on the card: in the driver's default
order, `mixed` cannot be created (its last 18 GB slice has nowhere to go). It
then refuses to run while any pod holds a GPU, unless you pass `--force`.

Re-slicing destroys every slice. Do it when nobody is running (check
`/hub/admin`), then:

1. The script writes `/etc/default/nvidia-mig-layout`, which the boot unit
   re-applies after every reboot.
2. It switches the device plugin to `migStrategy=mixed` whenever the slices
   differ in size. Each size then has its own resource name, e.g.
   `nvidia.com/mig-3g.71gb`.
3. It prints a `gpu_pools:` block. Paste it into the catalog and point each
   size's `gpu.resource` at one of those names. If the catalog and the node
   disagree, every GPU start is refused, or worse, left Pending.

The hub refuses a start when that size has no free slice, instead of leaving
a pod Pending for ten minutes. The spawn page shows how many are free.

## Templates

A template is a curated image built **from** the notebook image. JupyterLab,
SSH, the port proxy and the welcome material are therefore in every one. It
adds an app:

| Template | What it adds | Opens at |
|---|---|---|
| `pytorch` | Nothing; the notebook image | `/lab` |
| `comfyui` | ComfyUI v0.40, in a venv sharing the notebook's PyTorch. Models and outputs go to `~/ComfyUI` | `/user/<name>/comfyui/`, started on first visit (~10 s) |
| `vllm` | vLLM 0.31 (PyTorch 2.13, CUDA 12.9) in its own venv. `zai-vllm <model>` serves an OpenAI-compatible API on port 8000 | `/user/<name>/proxy/8000/v1` |

`gpu_required: true` hides a template from CPU-only sizes, and the hub
refuses that pair over the API too.

To add a template:

1. Create `images/templates/<name>/Dockerfile` with `FROM ${BASE}`.
2. Register any app with `c.ServerProxy.servers.update(...)` appended to
   `/etc/jupyter/jupyter_server_config.py`. Use `update`, not assignment, so
   it merges with the SSH entry already there.
3. Add a `templates:` entry in the catalog.
4. Run `TEMPLATES=<name> scripts/04-images.sh`.

Customers cannot bring their own image. On a GPU node shared with other
customers, an arbitrary image is the largest risk this platform could take on.

**Two constraints to know about:**

- **CUDA version.** The host driver (575) supports CUDA ≤ 12.9. vLLM ≥ 0.27
  on PyPI is built for CUDA 13 and will not run here. The image installs
  vLLM's own cu129 builds, plus a CPU build of `torchcodec`, the one
  dependency with no cu129 build. Upgrade the driver before chasing a newer
  CUDA.
- **Disk.** Home directories are 10 GB, which is too small for most model
  weights. A 7B model in fp16 is ~15 GB. Until customers can buy larger
  volumes, put popular models in `/srv/jupyter-shared/datasets`, which is
  `~/shared`, read-only in every server. Then
  `zai-vllm ~/shared/<model-dir>` loads them without anyone downloading
  anything.

## Reaching a server from outside

All of this goes through the hub's own URL and authentication, so the
Cloudflare tunnel stays the only way in. Customers create a JupyterHub API
token at `/hub/token`.

**SSH, scp, rsync, VS Code Remote-SSH.**

```bash
curl -fsSLo zai https://<host>/hub/zai/cli && chmod +x zai
./zai login https://<host>
./zai ssh-config >> ~/.ssh/config     # once
ssh zai
```

1. `ProxyCommand zai ssh-proxy` opens a websocket to `/user/<name>/ssh/`,
   authenticated with the token.
2. jupyter-server-proxy starts `zai-sshd` on demand inside the customer's
   server and bridges the websocket to it.
3. sshd runs as the notebook user and accepts only keys in that user's
   `~/.ssh/authorized_keys`. That makes two independent checks: the token,
   then the key.
4. The host key lives in the home volume, so it survives restarts.
5. The client pings every 25 s, because Cloudflare drops idle websockets
   after ~100 s.

Tested with the notebook image: login, scp (checksums verified), and `-L`
port forwarding. A websocket without a token is refused with 403.

**Web ports.** Anything listening in the server is reachable at
`https://<host>/user/<name>/proxy/<port>/`:

- In a browser, the hub login is enough.
- From code, send `Authorization: Bearer <token>`. JupyterHub accepts
  `Bearer`, so OpenAI-compatible clients work with the token as their API
  key:

  ```python
  OpenAI(base_url="https://<host>/user/alice/proxy/8000/v1", api_key="<token>")
  ```

- Without a token, the request is redirected to login. There are no public
  unauthenticated ports.

**Command-line tool and API.** `zai sizes | start | stop | status | balance |
url | ssh-config`. It is a single Python 3 file with no dependencies. It uses
the standard JupyterHub REST API (`POST /hub/api/users/<name>/server` with
`{"profile": "<size>", "template": "<template>"}`) plus:

| Endpoint | Returns |
|---|---|
| `GET /hub/api/zai/catalog` | Sizes, prices, free GPUs, templates |
| `GET /hub/api/zai/me` | Balance, running sessions, cost so far |
| `POST /hub/api/zai/credits` | Admin top-up (above) |

A refused start comes back as HTTP 402 (not enough credit) or 503 (no free
GPU of that size), with a message saying which.

## Not yet done

- **Online payments.** Credit is added by an admin or through
  `/api/zai/credits`; there is no checkout page yet.
- **Bigger or extra volumes**, and charging for storage.
- **Serverless endpoints** are built as an MVP; see
  [SERVERLESS.md](SERVERLESS.md). Workers are `kind='worker'` sessions in the
  same ledger, and share GPU capacity with notebooks through `gpus_in_use`.
