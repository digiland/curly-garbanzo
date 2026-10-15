#!/usr/bin/env bash
# Change how the GPU is cut into MIG slices -- i.e. which GPU sizes customers
# can buy. Re-slicing destroys every slice, so it refuses while anything is
# using the GPU.
#
#   sudo scripts/11-gpu-layout.sh --dry-run mixed     # check it fits; change nothing
#   sudo scripts/11-gpu-layout.sh mixed
#   sudo scripts/11-gpu-layout.sh "3g.71gb 1g.18gb*4"
#
# Presets (H200 141 GB):
#   team    1g.18gb*7                      7 x 18 GB      (the original layout)
#   mixed   3g.71gb 2g.35gb 1g.18gb*2      71 + 35 + 18 + 18 GB
#   large   3g.71gb*2                      2 x 71 GB
#   whole   7g.141gb                       the full GPU as one 141 GB device
#
# A layout with one profile keeps the device plugin on migStrategy=single
# (every slice is `nvidia.com/gpu`). More than one profile switches it to
# migStrategy=mixed, where each size has its own resource, e.g.
# `nvidia.com/mig-3g.71gb`. Either way the script ends by printing the
# gpu_pools block for hub-ext/catalog.yaml: the hub's sizes must name those
# resources, or every GPU start is refused (or, worse, left Pending).
set -euo pipefail
cd "$(dirname "$0")/.."
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
NVDP_VERSION="${NVDP_VERSION:-0.20.0}"

DRY=0 FORCE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1; shift ;;
        --force)   FORCE=1; shift ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        *) break ;;
    esac
done
case "${1:-}" in
    team)  LAYOUT="1g.18gb*7" ;;
    mixed) LAYOUT="3g.71gb 2g.35gb 1g.18gb*2" ;;
    large) LAYOUT="3g.71gb*2" ;;
    whole) LAYOUT="7g.141gb" ;;
    "")    echo "usage: $0 [--dry-run] [--force] team|mixed|large|whole|\"<layout>\""; exit 1 ;;
    *)     LAYOUT="$1" ;;
esac

echo "==> layout: $LAYOUT"
CGI=$(MIG_LAYOUT_FILE=/nonexistent MIG_LAYOUT="$LAYOUT" host/mig-setup.sh --plan) \
    || { echo "rejected (see above)"; exit 1; }
echo "    fits; placements $CGI"

PROFILES=$(echo "$LAYOUT" | tr ', ' '\n\n' | sed 's/\*.*//' | grep . | sort -u)
if [ "$(echo "$PROFILES" | wc -l)" -gt 1 ]; then STRATEGY=mixed; else STRATEGY=single; fi
echo "    device plugin: migStrategy=$STRATEGY"

# Anything holding a GPU dies when its slice is destroyed -- and a notebook
# would be killed mid-session without being told.
HOLDERS=$(kubectl get pods -A -o json | python3 -c '
import json, sys
for p in json.load(sys.stdin)["items"]:
    if p["status"].get("phase") not in ("Running", "Pending"):
        continue
    for c in p["spec"].get("containers", []):
        lim = (c.get("resources") or {}).get("limits") or {}
        if any(k.startswith("nvidia.com/") for k in lim):
            print(p["metadata"]["namespace"] + "/" + p["metadata"]["name"]); break
')
if [ -n "$HOLDERS" ]; then
    echo "    pods using the GPU now:"; echo "$HOLDERS" | sed 's/^/      /'
    if [ "$DRY" -eq 0 ] && [ "$FORCE" -eq 0 ]; then
        echo "Refusing: stop these first (/hub/admin), or --force to kill them."; exit 1
    fi
fi
[ "$DRY" -eq 1 ] && { echo "dry run: nothing changed"; exit 0; }

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }

echo "==> writing /etc/default/nvidia-mig-layout and the boot unit"
printf '# Written by scripts/11-gpu-layout.sh on %s\nMIG_LAYOUT="%s"\n' "$(date -u +%FT%TZ)" "$LAYOUT" \
    > /etc/default/nvidia-mig-layout
install -m 0755 host/mig-setup.sh /usr/local/bin/mig-setup.sh
install -m 0644 host/nvidia-mig-layout.service /etc/systemd/system/nvidia-mig-layout.service
systemctl daemon-reload

echo "==> re-slicing"
/usr/local/bin/mig-setup.sh
nvidia-smi -L

echo "==> device plugin"
helm upgrade nvdp nvdp/nvidia-device-plugin -n nvidia-device-plugin --version "$NVDP_VERSION" \
    --reuse-values --set migStrategy="$STRATEGY" --wait
# The plugin only enumerates devices at start; a re-slice under a running
# plugin leaves it advertising slices that no longer exist.
kubectl -n nvidia-device-plugin delete pod --all --wait=true
WANT=$(echo "$LAYOUT" | tr ', ' '\n\n' | awk -F'*' 'NF{n+=($2==""?1:$2)} END{print n}')
for i in $(seq 1 60); do
    GOT=$(kubectl get node -o json | python3 -c '
import json, sys
print(sum(int(v) for n in json.load(sys.stdin)["items"] for k, v in n["status"]["allocatable"].items() if k.startswith("nvidia.com/")))')
    [ "$GOT" -eq "$WANT" ] && break
    sleep 3
done
[ "$GOT" -eq "$WANT" ] || { echo "node advertises $GOT GPU devices, expected $WANT"; exit 1; }

echo
echo "Done. Put this in hub-ext/catalog.yaml (gpu_pools), point each size's"
echo "gpu.resource at one of these names, then run scripts/05-jupyterhub.sh:"
echo
echo "gpu_pools:"
kubectl get node -o json | python3 -c '
import json, sys
tot = {}
for n in json.load(sys.stdin)["items"]:
    for k, v in n["status"]["allocatable"].items():
        if k.startswith("nvidia.com/") and int(v):
            tot[k] = tot.get(k, 0) + int(v)
for k, v in sorted(tot.items()):
    print(f"  {k}: {v}")'
