# Serverless GPU endpoints

**Status: MVP built** (`serverless/`, `k8s/serverless.yaml`,
`scripts/12-serverless.sh`). Not yet deployed. The first half of this page is
how it works and how to use it. The second half is the research and design it
came from, kept for the later phases.

## How it works

```
customer --HTTPS--> tunnel --> hub proxy --/v2/--> gateway :8080
hub /hub/endpoints page ---------------------------> gateway :8082 (hub only)
                                                     | queue (SQLite)
                                                     | admission + billing --> hub /hub/api/zai/workers/*
                                                     | create/delete pods --> Kubernetes API
worker pod (template image + owner's home) <--:8081-- RunPod worker protocol
```

- **An endpoint** is a handler directory in its owner's home, e.g.
  `~/endpoints/echo/handler.py`, plus a size and a template. For now an
  admin creates endpoints for customers.
- **Customers send jobs** to `POST /v2/<endpoint>/run` (or `/runsync`) with
  their JupyterHub token. These are the same calls as RunPod's queue
  endpoints. Statuses: `IN_QUEUE`, `IN_PROGRESS`, `COMPLETED`, `FAILED`,
  `CANCELLED`, `TIMED_OUT`.
- **Workers start on demand.** The gateway starts one when jobs are waiting
  and none is free, up to the endpoint's `max_workers`. A worker is a pod
  running the endpoint's command in the template image, with the owner's
  home mounted. It is stopped after `idle_timeout` seconds without work.
- **Customers' RunPod handlers run unchanged.** Workers speak RunPod's worker
  protocol, so `runpod.serverless.start()` works, either through the
  dependency-free `zai_serverless` module (mounted into every worker and
  notebook) or with the real SDK installed.
- **The hub admits every worker.** It applies the same credit and GPU checks
  as for a notebook, plus a cap (`serverless.max_gpus` in the catalog) on how
  many GPUs workers may hold together. With the shipped catalog that is 2, so
  serverless can never take more than 2 of the 7 slices.
- **Billing** is per second, at the size's hourly price, from pod creation to
  deletion. Cold start and idle time count, as on RunPod. The charges show on
  the owner's account page like a notebook session.
- **When credit runs out**, queued and running jobs fail with
  `"Credit ran out."`, the owner's workers stop, and new jobs get 402.

## Using it

**Admin, once per endpoint.** The owner must have started a server once, so
that their home volume exists.

```bash
zai endpoint create --owner alice --name echo --size gpu-18 --template pytorch \
    --dir endpoints/echo --max-workers 1 --idle-timeout 60
```

**The Endpoints page** (`/hub/endpoints`, in the nav) shows each endpoint's
URL with copyable curl and Python examples, its queue, workers, spend this
month and recent jobs (metadata only, never inputs or outputs). Owners can
send a test job, cancel jobs and empty the queue. Admins see every endpoint
and can create and delete them there too. The hub renders the page and asks
the gateway on the user's behalf over a hub-only port (`:8082`, which only the
hub pod may reach, authenticated by the hub-gateway service token), and the
gateway applies the same owner-or-admin checks as for an API token.

**Customer.** Write and test the handler in a notebook or terminal, then call
it from anywhere:

```python
# ~/endpoints/echo/handler.py
import zai_serverless as runpod          # or `import runpod` after pip install runpod

def handler(job):
    return {"echo": job["input"]}

runpod.serverless.start({"handler": handler})
```

```bash
python handler.py --test_input '{"input": {"hi": 1}}'   # runs once, locally
zai run <endpoint> '{"hi": 1}' --sync                    # through the gateway
curl -H "Authorization: Bearer $TOKEN" -d '{"input": {"hi": 1}}' https://<host>/v2/<endpoint>/runsync
```

**What handlers can do:**

- Sync, async and generator handlers. With a generator, each yield is
  streamed and read with `GET /v2/<endpoint>/stream/<job>`.
- Return `{"error": "..."}` to fail the job.
- Return `{"refresh_worker": true}` to get a fresh worker for the next job.
- `/runsync` waits up to 90 s, below Cloudflare's ~120 s limit. After that it
  returns the job id with `IN_QUEUE`/`IN_PROGRESS`; poll `/status/<job>`.

## Data handling

- **Input is checked before it is stored.** It must be UTF-8 JSON without
  NaN/Infinity, at most 10 MB, with only `input` and `policy` allowed at the
  top level. `webhook` is refused rather than silently ignored.
- **Endpoint settings are checked too.** Size and template must exist and
  fit together, the working directory must stay inside the owner's home, the
  command must be one line, environment variables must have valid names (and
  reserved names are refused), and timeouts must fall within fixed limits.
- **Results are size-limited.** Over 20 MB, the job fails with a clear error.
- **Retention.** A job's input, output and stream are deleted 30 minutes
  after it finishes (1 minute for `/runsync`), as on RunPod. Queued jobs
  expire after their `ttl` (default 24 h). Stopped workers' records go after
  a day.
- **No tokens are stored.** Customer tokens are checked with the hub and
  cached in memory, keyed by hash. Worker secrets are stored as SHA-256
  hashes. Environment variable values are never returned by the API, only
  their names.
- **Logs** record request paths and status codes, never job inputs or
  outputs.
- **Money records stay.** Charges are rows in the hub's ledger, like
  notebook sessions, and are kept.

## Tested

The suite ran the real hub (with the platform module), the real gateway and
real worker processes, with a fake Kubernetes API that runs each pod as a
local process. 47 checks, plus the `zai` commands, passed:

- **Validation:** of input and endpoints.
- **Running jobs:** cold start, warm `/runsync`, handler errors, streaming,
  and an **unmodified runpod-python 1.12.0 handler**.
- **Timeouts and cancellation:** execution timeout (the worker is killed),
  cancelling queued and running jobs.
- **Sharing the GPU with notebooks:** a GPU job waits while a notebook holds
  the slice and runs once it is freed. A notebook is refused while a worker
  holds it.
- **Workers and billing:** idle workers are reaped, charges posted, the stop
  when credit runs out, the 402.
- **Data:** results purged after their TTL, no tokens in the database,
  orphaned pods removed.

**On the real cluster** (2026-10-15, a staging copy of the hub in namespace
`zai-staging` on the production node, not exposed through the tunnel), 15
more checks passed:

- **A real GPU worker:** an H200 MIG 1g.18gb slice, PyTorch on CUDA. The cold
  start took 5.5 s from the request to the result, with the image already on
  the node. A warm `/runsync` took 63 ms.
- **The job lifecycle:** async `/run`, handler errors, execution timeout,
  cancelling a running job, streaming, an unmodified runpod 1.12.0 handler,
  `/health`, and refusing a bad token, NaN input and webhooks.
- **Network policies:** from a worker, only the gateway's worker port, DNS
  and the internet are reachable. Both hubs, the production proxy, the
  Kubernetes API, cloud metadata and the node are not.
- **Billing** with `enforce: true`: a job running when credit hit zero failed
  within 8 s, its worker was deleted, and new jobs got 402.
- **The GPU cap:** an endpoint allowing 3 workers got 2. The other jobs
  waited for a free worker.

Two problems found there, both fixed:

- **Kubelet deleted the worker image.** The node's disk is above kubelet's
  85% image-GC threshold, so an image that no container is using is deleted,
  and with `pullPolicy: Never` it cannot come back. The import scripts now pin
  imported images (`io.cri-containerd.pinned`), which exempts them from GC.
- **A worker that can never start was kept.** The gateway waited
  `WORKER_START_TIMEOUT` (15 min), holding a GPU cap slot and billing the
  owner. It now deletes a pod stuck in an image or container-config error at
  once, sets the endpoint's `lastError`, and fails its queued jobs with the
  reason.

Known: after a top-up, new jobs can still get 402 for up to 15 s, until the
gateway next fetches balances.

## Deploying

1. Run `scripts/05-jupyterhub.sh`. It registers the gateway as a hub service
   (the chart generates its token) and routes `/v2/`.
2. Run `scripts/12-serverless.sh`. It builds the gateway image and creates
   the runtime ConfigMap, the gateway, its permissions and its network
   policies.

Nothing uses the GPU until an endpoint gets a job.

## Not in the MVP

- **Webhooks.** Clients poll `/status` instead.
- **Warm workers.** Nothing is kept running when there are no jobs.
- **Pre-emption.** Notebooks are protected by `max_gpus` instead.
- **Load-balanced endpoints.**
- **Customers creating their own endpoints.**
- **A model cache with its own quota.**
- **Custom images.**

These are phases 2–3 below.

## Research: what RunPod Serverless is

There are two endpoint types ([overview](https://docs.runpod.io/serverless/overview)):

- **Queue-based.** Your container runs a `handler(event)`, registered with
  `runpod.serverless.start()`. Requests are queued and retried automatically.
- **Load-balanced.** Requests go straight to your own HTTP server. Workers
  report readiness on `/ping`. There is no queue and no retries, a 5.5-minute
  cap per request and a 30 MB payload limit
  ([docs](https://docs.runpod.io/serverless/load-balancing/overview)).

**Operations.** The API is `POST /run` (async), `POST /runsync` (waits),
`GET /status/{id}`, plus `/stream`, `/cancel`, `/retry`, `/purge-queue` and
`/health`. The request body is
`{input, webhook?, policy: {executionTimeout (600 s), ttl (24 h)}}`. Results
are kept 30 minutes after `/run` and 1 minute after `/runsync`
([operations](https://docs.runpod.io/serverless/endpoints/operations)).

**Scaling.** "Active" workers are always warm (default 0), with up to 3
workers by default. Idle timeout defaults to 5 s. Scaling is driven either by
queue delay or by request count. FlashBoot keeps worker state after
spin-down ([configuration](https://docs.runpod.io/serverless/endpoints/endpoint-configurations)).

**Billing** is per second, from when a worker starts until it fully stops:
model load, execution and idle timeout all count. Image pulls do not
([pricing](https://docs.runpod.io/serverless/pricing)).

**Cold starts.** FlashBoot restores are claimed at under 200 ms. Normal cold
starts are 30 s to 2 minutes. Loading a 7B model's weights takes ~15 s
([blog](https://www.runpod.io/blog/serverless-gpu-cold-starts-flashboot)).

**Worker SDK.** [runpod-python](https://github.com/runpod/runpod-python)
workers pull jobs from the URL in `RUNPOD_WEBHOOK_GET_JOB` and post results
to `RUNPOD_WEBHOOK_POST_OUTPUT`. Our own queue could serve unmodified RunPod
worker images. That is an internal protocol RunPod can change, so the SDK
version would have to be pinned and tested.

## Constraints on this stack

- **Cloudflare timeout.** Cloudflare ends proxied requests after ~120 s on
  non-Enterprise plans
  ([error 524](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524)).
  A synchronous request cannot wait out a 30–90 s cold start plus a long job.
  `/runsync` has to return a job id and let the client poll, as RunPod's does.
- **MIG.** Every option below just requests an `nvidia.com/mig-*` resource.
  None of them can re-slice the GPU while anything is running on it.
- **Capacity.** Seven slices (or fewer, larger ones). Scale-to-zero and
  cold-start time are the whole trade-off.

## Options considered

| Option | Status | Fit (recommendation) |
|---|---|---|
| Knative Serving | v1.23; only the latest 2 minors supported ([releases](https://github.com/knative/serving/releases)) | Routes by Host header; our proxy routes by path. No queue or `/status`. Upgrade every ~4 months. Medium–high burden. |
| KServe | v0.19/0.20; scale-to-zero only in Knative mode ([docs](https://kserve.github.io/website/docs/model-serving/predictive-inference/autoscaling/hpa-autoscaler)) | Knative's costs plus an operator. Built for models, not user handlers. Poor fit. |
| KEDA + HTTP add-on | KEDA 2.21; HTTP add-on still beta ([repo](https://github.com/kedacore/http-add-on)) | Plain KEDA scaling on queue length is a reasonable scaler for a queue design. |
| Ray Serve | `min_replicas=0` supported ([guide](https://docs.ray.io/en/latest/serve/autoscaling-guide.html)) | Heavy head node; one Ray cluster for many customers weakens isolation. Poor fit. |
| OpenFaaS | Scale-to-zero is Pro-only; CE restricts commercial use ([comparison](https://docs.openfaas.com/openfaas-pro/comparison/)) | Ruled out for a paid service. |
| Fission | v1.27 | Built for CPU functions. Poor fit. |
| Triton / Dynamo | Dynamo 1.5 adds an operator ([notes](https://docs.nvidia.com/dynamo/reference/releases/v1-5-0)) | Inference fleets, not a multi-tenant function service. |
| vLLM production-stack | Chart 0.1.13 ([releases](https://github.com/vllm-project/production-stack/releases)) | Good later for a *platform-run* shared LLM endpoint. LLM-only. |
| **Custom queue + controller** | — | **Best fit.** Roughly 1–2k lines of Python. Reuses the hub's tokens, ledger and route pattern, and handles the 120 s limit natively. The code is ours to maintain, but there is little dependency churn. |

## Cold starts here

- **Image pull.** Free on one node: images sit in local containerd.
- **Pod create.** About 2–5 s.
- **Weights.** Keep them in a node-local model cache (a hostPath with its own
  XFS project quota), not in images. With 235 GB of RAM, recently used
  weights stay in the page cache, so reloading a 7–8B model takes seconds.
- **vLLM warm-up.** CUDA-graph capture and `torch.compile` add 20–60 s.
  Persist the compile cache; offer `--enforce-eager` for short-lived workers.
- **Estimated totals** (recommendation; to be measured): 10–30 s for small
  handlers, 30–90 s for vLLM with a 7–8B model.
- **Disk** is the hard limit: ~100 GB free has to hold images and the model
  cache. Cap the cache at ~60 GB with LRU eviction.
- **Warm workers** hold a slice around the clock, so charge them the full
  hourly rate, as RunPod does. vLLM's sleep mode still holds the slice, so it
  saves no capacity.

## Capacity policy (recommendation)

**One place decides admission.** Use the hub's `gpus_in_use()`, already shared
with notebooks. A worker is admitted only if a slice is free and the owner's
credit covers N minutes.

**PriorityClasses:**

- `interactive` and `serverless-active` (reserved, paid around the clock)
  at the same high priority.
- `serverless-flex` lower, so a notebook start can pre-empt a flex worker.
  The interrupted job is re-queued, and minutes already used are still
  charged.

**Hard caps:**

- Flex workers may use at most 2–3 slices in total.
- One worker per user by default.
- A maximum queue depth.
- A 600 s execution timeout.

**Kueue** only when there is more than one node.

## Proposed design

**Components:**

- **`serverless-gateway`.** FastAPI in its own namespace, with network
  policies modelled on `agents/base.yaml`.
  - Routed by the hub's proxy at `/v2/` (as `/agents/` is today).
  - Authenticates `Authorization: Bearer <JupyterHub token>`, checked against
    `/hub/api/user` and cached for 60 s.
  - Uses its own scope (`custom:serverless:invoke`), so an invoke token
    cannot also start servers.
- **Queue and jobs.** SQLite first; Redis only if throughput demands it.
  Inputs are capped at 10 MB; bigger payloads go through object storage.
- **Controller loop.** Every 2 s:
  - desired workers = `min(max, active + ceil((queued + running) / scaler))`,
    limited by free slices and credit;
  - scale back to `active` after an idle timeout of 30 s (not RunPod's 5 s,
    given our cold starts).
- **Workers.** Built from curated templates. Non-root, no service-account
  token, traffic accepted only from the gateway. They pull jobs over the
  RunPod SDK protocol.

**API (RunPod-compatible):**

- `POST /v2/{endpoint}/run`, `POST /v2/{endpoint}/runsync`.
  `runsync` waits up to ~90 s, then returns `IN_QUEUE`/`IN_PROGRESS` and the
  job id.
- `GET /status/{id}`, `POST /cancel/{id}`, `GET /health`.
- Job states: `IN_QUEUE`, `IN_PROGRESS`, `COMPLETED`, `FAILED`, `CANCELLED`,
  `TIMED_OUT`.
- Being compatible lets customers bring RunPod clients and worker images.

**Billing:**

- Reject `/run` when the balance cannot cover the estimated job.
- Record each worker's life (pod running → terminated) as a `sessions` row
  with `kind='worker'`, charged per second at the slice's rate. Load, run and
  idle time all count, as on RunPod.
- At zero credit, scale the owner's endpoints to zero and fail their queued
  jobs.

**Phases:**

1. **MVP, 2–3 weeks.** Queue endpoints only; endpoints created by admins
   from templates; flex workers; SQLite; per-second billing;
   `zai endpoint` commands.
2. **Hardening.** Warm workers, pre-emption and re-queue, webhooks,
   `/stream`, the model cache, GPU metrics (DCGM).
3. **Load-balanced endpoints.** The gateway proxies to the endpoint's
   service and holds requests through a cold start of up to ~90 s.
4. **Platform-run LLM endpoint.** A shared vLLM endpoint, billed per token.

**Risks:**

- Cold starts close to Cloudflare's 120 s limit.
- Flex workers pre-empted mid-job.
- Disk exhaustion.
- Abuse: crypto-mining, egress, oversized inputs. Mitigated by curated
  templates, network policy and timeouts.
- The hub as a dependency: token checks fail while it is down. Cache them
  briefly and fail closed.
- MIG fragmentation under mixed layouts.
- RunPod changing its worker protocol.

## Already prepared in steps 1–5

- `sessions.kind` exists (`notebook`; workers will be `worker`), and each
  session records its own price.
- Capacity is counted in one function, `gpus_in_use()`, keyed by GPU pool.
  Workers just have to record sessions there.
- Templates are images built from the notebook image, so an app built for
  one can also be a worker image.

## Still to do before serverless

- Custom token scopes for invoke-only tokens.
- PriorityClasses.
- A model-cache directory with its own quota.
