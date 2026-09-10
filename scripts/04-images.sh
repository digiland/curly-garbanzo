#!/usr/bin/env bash
# Build the hub and singleuser images.
#
# Single node (default): import straight into k3s containerd. k3s uses its OWN
# containerd, not the Docker daemon, hence the save|import.
#
# Cluster: set REGISTRY and the images are pushed instead. This is mandatory on
# more than one node -- an imported image exists only on the node that imported
# it, and the cluster overlay's pullPolicy: IfNotPresent will fail everywhere
# else. (The base file's pullPolicy: Never does not even attempt a pull.)
#
#   REGISTRY=ghcr.io/digiland scripts/04-images.sh
set -euo pipefail
cd "$(dirname "$0")/.."
CHART_VERSION="${CHART_VERSION:-4.4.2}"
SINGLEUSER_BASE="${SINGLEUSER_BASE:-quay.io/jupyter/pytorch-notebook:cuda12-latest}"
REGISTRY="${REGISTRY:-}"

if [ -n "$REGISTRY" ]; then
    HUB_TAG="${REGISTRY}/k8s-hub-native:${CHART_VERSION}"
    SU_TAG="${REGISTRY}/pytorch-caimex:cuda12"
else
    HUB_TAG="local/k8s-hub-native:${CHART_VERSION}"
    SU_TAG="local/pytorch-caimex:cuda12"
fi

echo "==> hub image (NativeAuthenticator)  -> ${HUB_TAG}"
docker build -t "$HUB_TAG" images/hub

echo "==> singleuser image (caimex CLI)    -> ${SU_TAG}"
docker build -t "$SU_TAG" images/singleuser

if [ -n "$REGISTRY" ]; then
    echo "==> pushing (docker login ${REGISTRY%%/*} first if this fails)"
    docker push "$HUB_TAG"
    docker push "$SU_TAG"
    echo
    echo "Set these in charts/jupyterhub-values-cluster.yaml:"
    echo "  hub.image.name        ${HUB_TAG%:*}"
    echo "  singleuser.image.name ${SU_TAG%:*}"
else
    docker save "$HUB_TAG" | k3s ctr images import -
    docker save "$SU_TAG"  | k3s ctr images import -
    echo "==> pre-pulling base so first spawn isn't a timeout"
    k3s ctr images pull "$SINGLEUSER_BASE" || true
    k3s ctr images ls | awk '{print $1}' | grep -E 'local/|pytorch-notebook' | sort -u
fi
