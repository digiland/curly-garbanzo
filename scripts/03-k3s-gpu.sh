#!/usr/bin/env bash
# k3s + NVIDIA device plugin with MIG support.
set -euo pipefail
NVDP_VERSION="${NVDP_VERSION:-0.20.0}"
EXPECT_GPUS="${MIG_COUNT:-7}"

if ! command -v k3s >/dev/null; then
    curl -sfL https://get.k3s.io | \
      INSTALL_K3S_EXEC="--disable traefik --disable servicelb --write-kubeconfig-mode 644" sh -
fi
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
command -v helm >/dev/null || curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

echo "==> waiting for node Ready"
for _ in $(seq 1 60); do kubectl get node --no-headers 2>/dev/null | grep -q ' Ready ' && break; sleep 4; done
kubectl get node

# k3s auto-detects nvidia-container-runtime and creates the `nvidia` RuntimeClass.
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
  --set migStrategy=single \
  --set runtimeClassName=nvidia \
  --set securityContext.privileged=true \
  --wait --timeout 5m

echo "==> waiting for GPUs to be advertised"
for _ in $(seq 1 30); do
    n=$(kubectl get node -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}' 2>/dev/null || echo 0)
    [ "${n:-0}" = "$EXPECT_GPUS" ] && break; sleep 5
done
echo "allocatable nvidia.com/gpu = ${n:-0} (expected $EXPECT_GPUS)"
[ "${n:-0}" = "$EXPECT_GPUS" ] || { echo "GATE FAILED - do not continue"; exit 1; }
