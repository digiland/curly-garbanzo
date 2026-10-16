#!/usr/bin/env bash
# Serverless endpoints (docs/SERVERLESS.md). Run after 05-jupyterhub.sh, which
# registers the gateway as a hub service (and so creates its API token) and
# routes /v2/ to it.
#
# Nothing starts on the GPU until an admin creates an endpoint and someone
# sends it a job; serverless.max_gpus in hub-ext/catalog.yaml caps how many
# slices workers may ever hold.
set -euo pipefail
cd "$(dirname "$0")/.."
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
TAG="${TAG:-local/zai-serverless:0.1}"

kubectl -n jupyterhub get secret hub -o jsonpath='{.data.hub\.services\.serverless\.apiToken}' | grep -q . \
  || { echo "no hub service token for 'serverless' yet: run scripts/05-jupyterhub.sh first"; exit 1; }

echo "==> gateway image -> $TAG"
docker build -t "$TAG" serverless
docker save "$TAG" | k3s ctr images import -
k3s ctr -n k8s.io images label "docker.io/$TAG" io.cri-containerd.pinned=pinned >/dev/null   # exempt from image GC (04-images.sh)

echo "==> worker runtime (zai_serverless.py, mounted into workers and notebooks)"
kubectl -n jupyterhub create configmap serverless-runtime \
  --from-file=serverless/runtime/zai_serverless.py --dry-run=client -o yaml | kubectl apply -f -

echo "==> gateway"
kubectl apply -f k8s/serverless.yaml
kubectl -n jupyterhub rollout restart deploy/serverless-gateway
kubectl -n jupyterhub rollout status deploy/serverless-gateway --timeout=120s

cat <<'MSG'

Check from outside (expects 401 -- reachable, wants a token):
  curl -s -o /dev/null -w '%{http_code}\n' https://<host>/v2/endpoints

Create an endpoint (admin token; the owner must have started a server once):
  zai endpoint create --owner alice --name echo --size cpu --dir endpoints/echo
MSG
