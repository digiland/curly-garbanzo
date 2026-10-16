#!/usr/bin/env bash
# Build the hub and singleuser images, and the template images in
# images/templates/ (ComfyUI, vLLM) that customers pick at spawn time.
#
# Templates build FROM the singleuser image, so they always rebuild after it.
# TEMPLATES="" skips them; TEMPLATES="comfyui" builds just that one. The tags
# must match the `image:` lines in hub-ext/catalog.yaml.
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
SU_VERSION="${SU_VERSION:-cuda12-r3}"
TEMPLATES="${TEMPLATES-comfyui vllm}"

if [ -n "$REGISTRY" ]; then
    HUB_TAG="${REGISTRY}/k8s-hub-native:${CHART_VERSION}"
    SU_TAG="${REGISTRY}/pytorch-caimex:${SU_VERSION}"
else
    HUB_TAG="local/k8s-hub-native:${CHART_VERSION}"
    SU_TAG="local/pytorch-caimex:${SU_VERSION}"
fi

echo "==> hub image (NativeAuthenticator)  -> ${HUB_TAG}"
docker build -t "$HUB_TAG" images/hub

echo "==> singleuser image (caimex CLI)    -> ${SU_TAG}"
docker build -t "$SU_TAG" images/singleuser

TPL_TAGS=()
for t in $TEMPLATES; do
    tag="${SU_TAG%%/pytorch-caimex:*}/zai-${t}:${SU_VERSION}"
    echo "==> template ${t}                     -> ${tag}"
    docker build --build-arg BASE="$SU_TAG" -t "$tag" "images/templates/${t}"
    TPL_TAGS+=("$tag")
done

if [ -n "$REGISTRY" ]; then
    echo "==> pushing (docker login ${REGISTRY%%/*} first if this fails)"
    docker push "$HUB_TAG"
    docker push "$SU_TAG"
    for tag in "${TPL_TAGS[@]}"; do docker push "$tag"; done
    echo
    echo "Set these in charts/jupyterhub-values-cluster.yaml:"
    echo "  hub.image.name        ${HUB_TAG%:*}"
    echo "  singleuser.image.name ${SU_TAG%:*}"
else
    docker save "$HUB_TAG" | k3s ctr images import -
    docker save "$SU_TAG"  | k3s ctr images import -
    # One save of all templates: they share the singleuser layers, which a
    # per-image save would export (and import) again for each.
    [ ${#TPL_TAGS[@]} -gt 0 ] && docker save "${TPL_TAGS[@]}" | k3s ctr images import -
    # pullPolicy: Never means a deleted image cannot come back by itself, and
    # kubelet's image GC deletes any image no container is using once the disk
    # passes 85%. Pinned images are exempt.
    for tag in "$HUB_TAG" "$SU_TAG" "${TPL_TAGS[@]}"; do
        k3s ctr -n k8s.io images label "docker.io/$tag" io.cri-containerd.pinned=pinned >/dev/null
    done
    echo "==> pre-pulling base so first spawn isn't a timeout"
    k3s ctr images pull "$SINGLEUSER_BASE" || true
    k3s ctr images ls | awk '{print $1}' | grep -E 'local/|pytorch-notebook' | sort -u
fi
