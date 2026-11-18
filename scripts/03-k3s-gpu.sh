#!/usr/bin/env bash
# k3s + NVIDIA device plugin with MIG support.
set -euo pipefail
NVDP_VERSION="${NVDP_VERSION:-0.20.0}"
# Device plugin MIG strategy. "single" exposes every slice as nvidia.com/gpu and
# needs one uniform layout. This box has two slice sizes plus whole GPUs, so it
# needs "mixed": nvidia.com/mig-1g.18gb, nvidia.com/mig-2g.35gb, nvidia.com/gpu.
MIG_STRATEGY="${MIG_STRATEGY:-single}"
# Total nvidia.com/* devices the node must advertise once the plugin runs.
EXPECT_DEVICES="${EXPECT_DEVICES:-${MIG_COUNT:-7}}"
# k3s state + containerd images. The default is /var/lib/rancher/k3s; on this
# box / is small (~19G free) so it points at the 12T data volume.
K3S_DATA_DIR="${K3S_DATA_DIR:-/var/lib/rancher/k3s}"
K3S_KUBECONFIG="${K3S_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

if ! command -v k3s >/dev/null; then
    mkdir -p "$K3S_DATA_DIR" "$(dirname "$K3S_KUBECONFIG")"
    curl -sfL https://get.k3s.io | \
      INSTALL_K3S_EXEC="--disable traefik --disable servicelb --write-kubeconfig-mode 644 --data-dir $K3S_DATA_DIR --write-kubeconfig $K3S_KUBECONFIG" sh -
fi
export KUBECONFIG="$K3S_KUBECONFIG"
command -v helm >/dev/null || curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

echo "==> waiting for node Ready"
for _ in $(seq 1 60); do kubectl get node --no-headers 2>/dev/null | grep -q ' Ready ' && break; sleep 4; done
kubectl get node

# k3s auto-detects nvidia-container-runtime and creates the `nvidia` RuntimeClass,
# but only a few seconds into startup -- retry rather than fail the first run.
for _ in $(seq 1 30); do
    kubectl get runtimeclass nvidia >/dev/null 2>&1 && break
    sleep 2
done
kubectl get runtimeclass nvidia >/dev/null 2>&1 \
  || { echo "ERROR: no 'nvidia' RuntimeClass - is nvidia-container-toolkit installed?"; exit 1; }

# REQUIRED: the chart's affinity expects Node Feature Discovery labels. Rather than
# run NFD for one known GPU node, label it directly. Re-apply if the node is rebuilt.
kubectl label node "$(kubectl get node -o jsonpath='{.items[0].metadata.name}')" \
    nvidia.com/gpu.present=true --overwrite

helm repo add nvdp https://nvidia.github.io/k8s-device-plugin >/dev/null 2>&1 || true
helm repo update nvdp
# privileged is REQUIRED under MIG: NVML cannot read the parent GPU from behind a
# MIG child otherwise, and the plugin crash-loops on "Insufficient Permissions".
helm upgrade -i nvdp nvdp/nvidia-device-plugin \
  --namespace nvidia-device-plugin --create-namespace \
  --version "$NVDP_VERSION" \
  --set migStrategy="$MIG_STRATEGY" \
  --set runtimeClassName=nvidia \
  --set securityContext.privileged=true \
  --wait --timeout 5m

# Total nvidia.com/* devices the node advertises, plus a breakdown for the log.
gpu_devices() {
    kubectl get node -o json | python3 -c '
import json, sys
tot = {}
for n in json.load(sys.stdin)["items"]:
    for k, v in n["status"]["allocatable"].items():
        if k.startswith("nvidia.com/") and int(v):
            tot[k] = tot.get(k, 0) + int(v)
print(sum(tot.values()))'
}
gpu_breakdown() {
    kubectl get node -o json | python3 -c '
import json, sys
tot = {}
for n in json.load(sys.stdin)["items"]:
    for k, v in n["status"]["allocatable"].items():
        if k.startswith("nvidia.com/") and int(v):
            tot[k] = tot.get(k, 0) + int(v)
for k, v in sorted(tot.items()):
    print("  %-32s %s" % (k, v))'
}

echo "==> waiting for GPUs to be advertised (strategy=$MIG_STRATEGY)"
for _ in $(seq 1 30); do
    n=$(gpu_devices 2>/dev/null || echo 0)
    [ "${n:-0}" = "$EXPECT_DEVICES" ] && break; sleep 5
done
gpu_breakdown
echo "total nvidia.com/* = ${n:-0} (expected $EXPECT_DEVICES)"
[ "${n:-0}" = "$EXPECT_DEVICES" ] || { echo "GATE FAILED - do not continue"; exit 1; }
