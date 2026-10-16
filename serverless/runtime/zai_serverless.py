"""Worker runtime for ZAI serverless endpoints. Standard library only.

A handler written for RunPod runs unchanged:

    import zai_serverless as runpod          # or: import runpod (pip install runpod)

    def handler(job):
        return {"echo": job["input"]}

    runpod.serverless.start({"handler": handler})

Supported: sync and async handlers, generator handlers (each yield is
streamed; `"return_aggregate_stream": True` also returns them all as the
output), returning {"error": ...}, and {"refresh_worker": True} to get a fresh
worker after the job.

Outside a worker -- in a notebook or terminal -- start() runs the handler
once on --test_input '<json>' (or ./test_input.json) and prints the result,
like the RunPod SDK does, so a handler can be tried before it is deployed.
"""

import asyncio
import inspect
import json
import os
import sys
import time
import traceback
import types
import urllib.error
import urllib.request

__all__ = ["start", "serverless"]

_WORKER = os.environ.get("RUNPOD_POD_ID", "local")


def _url(name, job_id=None):
    u = os.environ[name].replace("$RUNPOD_POD_ID", _WORKER).replace("$ID", job_id or _WORKER)
    return u


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None             # a redirect would carry the worker secret elsewhere


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request(method, url, body=None, timeout=60):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": os.environ.get("RUNPOD_AI_API_KEY", ""),
        "Content-Type": "application/json",
    })
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _call(handler, job):
    out = handler(job)
    if inspect.isawaitable(out):
        out = asyncio.run(_await(out))
    return out


async def _await(x):
    return await x


def _result(output):
    """Shape a handler's return value the way RunPod's SDK does."""
    res = {}
    if isinstance(output, dict):
        output = dict(output)
        err = output.pop("error", None)
        refresh = output.pop("refresh_worker", None)
        if output:
            res["output"] = output
        if err:
            res["error"] = str(err)
        if refresh:
            res["stopPod"] = True
    else:
        res["output"] = output
    return res


def _run_job(config, job):
    handler = config["handler"]
    try:
        if inspect.isgeneratorfunction(handler) or inspect.isasyncgenfunction(handler):
            chunks = []
            gen = handler(job)
            if inspect.isasyncgen(gen):
                async def drain():
                    async for c in gen:
                        _stream(job, c)
                        chunks.append(c)
                asyncio.run(drain())
            else:
                for c in gen:
                    _stream(job, c)
                    chunks.append(c)
            res = {"output": chunks} if config.get("return_aggregate_stream") else {}
        else:
            res = _result(_call(handler, job))
    except Exception as e:
        res = {"error": json.dumps({
            "error_type": type(e).__name__,
            "error_message": str(e),
            "error_traceback": traceback.format_exc()[-8000:],
        })}
    if config.get("refresh_worker"):
        res["stopPod"] = True
    return res


def _stream(job, chunk):
    if "RUNPOD_WEBHOOK_POST_STREAM" in os.environ:
        _request("POST", _url("RUNPOD_WEBHOOK_POST_STREAM", job["id"]) + "&isStream=true", {"output": chunk})


def _local(config):
    raw = None
    if "--test_input" in sys.argv:
        raw = sys.argv[sys.argv.index("--test_input") + 1]
    elif os.path.exists("test_input.json"):
        with open("test_input.json") as f:
            raw = f.read()
    if raw is None:
        print("zai_serverless: not running as a worker. Try it with --test_input '{\"input\": {...}}'"
              " or a test_input.json file.", file=sys.stderr)
        sys.exit(2)
    job = json.loads(raw)
    job.setdefault("id", "local-test")
    print(json.dumps(_run_job(config, job), indent=2, ensure_ascii=False))


def start(config):
    if "RUNPOD_WEBHOOK_GET_JOB" not in os.environ:
        return _local(config)
    print(f"zai_serverless: worker {_WORKER} ready", flush=True)
    backoff = 1
    while True:
        try:
            code, raw = _request("GET", _url("RUNPOD_WEBHOOK_GET_JOB") + "&job_in_progress=0", timeout=90)
        except OSError as e:
            print(f"zai_serverless: gateway unreachable ({e}); retrying", file=sys.stderr, flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        backoff = 1
        if code == 204 or not raw:
            continue
        if code == 410:                      # the gateway is stopping this worker
            return
        if code != 200:
            print(f"zai_serverless: job-take returned {code}", file=sys.stderr, flush=True)
            time.sleep(2)
            continue
        jobs = json.loads(raw)
        for job in (jobs if isinstance(jobs, list) else [jobs]):
            t0 = time.time()
            res = _run_job(config, job)
            for attempt in range(3):
                try:
                    code, _ = _request("POST", _url("RUNPOD_WEBHOOK_POST_OUTPUT", job["id"]) + "&isStream=false", res)
                    if code < 500:
                        break
                except OSError:
                    pass
                time.sleep(1 + attempt)
            print(f"zai_serverless: job {job['id']} done in {time.time() - t0:.1f}s", flush=True)
            if res.get("stopPod"):
                return


# `import zai_serverless as runpod` then `runpod.serverless.start(...)`.
serverless = types.SimpleNamespace(start=start)
