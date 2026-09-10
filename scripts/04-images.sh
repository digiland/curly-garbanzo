#!/usr/bin/env bash
# Build the hub and singleuser images and import them into k3s containerd.
# k3s uses its OWN containerd, not the Docker daemon, hence the save|import.
set -euo pipefail
cd "$(dirname "$0")/.."
CHART_VERSION="${CHART_VERSION:-4.4.2}"
SINGLEUSER_BASE="${SINGLEUSER_BASE:-quay.io/jupyter/pytorch-notebook:cuda12-latest}"

echo "==> hub image (NativeAuthenticator)"
docker build -t "local/k8s-hub-native:${CHART_VERSION}" images/hub
docker save "local/k8s-hub-native:${CHART_VERSION}" | k3s ctr images import -

echo "==> singleuser image (caimex CLI)"
docker build -t local/pytorch-caimex:cuda12 images/singleuser
docker save local/pytorch-caimex:cuda12 | k3s ctr images import -

echo "==> pre-pulling base so first spawn isn't a timeout"
k3s ctr images pull "$SINGLEUSER_BASE" || true
k3s ctr images ls | awk '{print $1}' | grep -E 'local/|pytorch-notebook' | sort -u
