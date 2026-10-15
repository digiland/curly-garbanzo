# Serverless GPU endpoints: research and proposed design

Status: **proposal, nothing built.** This is step 6, after billing, GPU sizes,
templates, remote access and the API ([PLATFORM.md](PLATFORM.md)). It records
what RunPod's serverless product is, what could deliver it on this stack, and
a recommended design. Claims about external projects link to their source and
were current as of October 2026. Everything marked *recommendation* is
judgement, not fact.

## What RunPod Serverless is

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
