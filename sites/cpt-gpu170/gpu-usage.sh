#!/usr/bin/env bash
# Who is on which GPU slice, and how much of each pool is left.
#
#   sudo sites/cpt-gpu170/gpu-usage.sh
#
# Three views:
#   1. pools     free/used per MIG profile, cluster-wide (every namespace, not
#                just JupyterHub -- other workloads can hold slices too)
#   2. slices    every MIG instance on every physical GPU, with its VRAM in use,
#                compute activity, and the pod holding it -- from the GPU
#                Operator's DCGM exporter, which labels a slice with its pod
#   3. waiting   user servers stuck Pending, i.e. people queued for a full pool
set -uo pipefail
export KUBECONFIG="${KUBECONFIG:-/etc/kubernetes/admin.conf}"

# Files, not variables: the cluster-wide pod list is far larger than the
# kernel's limit for an environment or argument list.
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
node=$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')
kubectl get node "$node" -o json > "$tmp/node.json"
kubectl get pods -A -o json > "$tmp/pods.json"
dcgm=$(kubectl -n gpu-operator-resources get svc nvidia-dcgm-exporter \
        -o jsonpath='{.spec.clusterIP}:{.spec.ports[0].port}' 2>/dev/null)
[ -n "$dcgm" ] && curl -s --max-time 5 "http://$dcgm/metrics" > "$tmp/metrics.txt"
touch "$tmp/metrics.txt"

python3 - "$tmp" <<'EOF'
import json, re, sys
from collections import defaultdict

tmp = sys.argv[1]
alloc = json.load(open(f"{tmp}/node.json"))["status"]["allocatable"]
pods = json.load(open(f"{tmp}/pods.json"))["items"]

# ---- 1. pools -------------------------------------------------------------
total = {k: int(v) for k, v in alloc.items() if k.startswith("nvidia.com/mig-")}
used, holders, waiting = defaultdict(int), defaultdict(list), []
for p in pods:
    phase = p["status"].get("phase")
    if phase not in ("Pending", "Running"):
        continue
    ns, name = p["metadata"]["namespace"], p["metadata"]["name"]
    user = p["metadata"].get("annotations", {}).get("hub.jupyter.org/username")
    who = f"{user} (jupyterhub)" if user else f"{ns}/{name}"
    for c in p["spec"]["containers"]:
        for k, v in (c.get("resources", {}).get("limits") or {}).items():
            if k.startswith("nvidia.com/mig-"):
                if phase == "Running":
                    used[k] += int(v); holders[k].append(who)
                else:
                    waiting.append((who, k, p["metadata"].get("creationTimestamp", "")))

print("POOLS")
print(f"  {'profile':<14} {'used':>5} {'free':>5} {'total':>6}")
for k in sorted(total):
    t, u = total[k], used[k]
    flag = "   <-- FULL" if u >= t else ""
    print(f"  {k.split('/')[1]:<14} {u:>5} {max(t-u,0):>5} {t:>6}{flag}")

# ---- 2. slices ------------------------------------------------------------
line_re = re.compile(r'^(DCGM_FI_[A-Z_]+)\{(.*)\} (\S+)$')
lab_re = re.compile(r'(\w+)="([^"]*)"')
slices = defaultdict(dict)
for line in open(f"{tmp}/metrics.txt").read().splitlines():
    m = line_re.match(line)
    if not m:
        continue
    metric, labels, value = m.group(1), dict(lab_re.findall(m.group(2))), m.group(3)
    if "GPU_I_ID" not in labels:
        continue
    key = (int(labels["gpu"]), int(labels["GPU_I_ID"]))
    s = slices[key]
    s["profile"] = labels.get("GPU_I_PROFILE", "?")
    if labels.get("pod"):
        s["pod"] = f"{labels.get('namespace','?')}/{labels['pod']}"
    if metric == "DCGM_FI_DEV_FB_USED":
        s["mem"] = float(value)
    elif metric == "DCGM_FI_DEV_FB_FREE":
        s["free"] = float(value)
    elif metric == "DCGM_FI_PROF_GR_ENGINE_ACTIVE":
        s["busy"] = float(value)

print("\nSLICES  (VRAM in use / slice size, compute busy, holder)")
if not slices:
    print("  DCGM exporter unreachable or reporting no MIG instances")
for (gpu, gi) in sorted(slices):
    s = slices[(gpu, gi)]
    tot = s.get("mem", 0) + s.get("free", 0)
    mem = f"{s.get('mem',0)/1024:5.1f}/{tot/1024:4.1f} GB" if tot else "      ?     "
    busy = f"{s['busy']*100:4.0f}%" if "busy" in s else "   ?"
    print(f"  GPU {gpu}  slice {gi:<2} {s['profile']:<9} {mem}  {busy}  {s.get('pod','-')}")

print("\nHOLDERS")
for k in sorted(holders):
    print(f"  {k.split('/')[1]}: {', '.join(sorted(holders[k]))}")
if not holders:
    print("  none")

# ---- 3. waiting -----------------------------------------------------------
print("\nWAITING FOR A SLICE (Pending)")
for who, k, since in sorted(waiting, key=lambda w: w[2]):
    print(f"  {who:<32} {k.split('/')[1]:<14} since {since}")
if not waiting:
    print("  nobody")
EOF
