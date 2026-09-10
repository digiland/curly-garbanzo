#!/usr/bin/env bash
# Deploy JupyterHub. Requires CAIMEX_API_KEY (see .env.example).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
CHART_VERSION="${CHART_VERSION:-4.4.2}"

kubectl create namespace jupyterhub --dry-run=client -o yaml | kubectl apply -f -

if [ -n "${CAIMEX_API_KEY:-}" ]; then
    kubectl -n jupyterhub create secret generic caimex-credentials \
      --from-literal=api-key="$CAIMEX_API_KEY" --dry-run=client -o yaml | kubectl apply -f -
else
    echo "WARNING: CAIMEX_API_KEY unset - caimex will be installed but unauthenticated"
    kubectl -n jupyterhub create secret generic caimex-credentials \
      --from-literal=api-key="" --dry-run=client -o yaml | kubectl apply -f -
fi

helm repo add jupyterhub https://hub.jupyter.org/helm-chart/ >/dev/null 2>&1 || true
helm repo update jupyterhub
helm upgrade -i jupyterhub jupyterhub/jupyterhub \
  --namespace jupyterhub --version "$CHART_VERSION" \
  -f charts/jupyterhub-values.yaml --wait --timeout 10m

kubectl -n jupyterhub get pods
cat <<'MSG'

NEXT: claim the admin account BEFORE exposing this publicly.
NativeAuthenticator auto-approves a user named `admin`, so whoever registers
first becomes administrator.

  kubectl -n jupyterhub port-forward svc/proxy-public 8080:http
  # from your laptop: ssh -L 8080:localhost:8080 root@<host>
  # browse http://localhost:8080/hub/signup and register as: admin
MSG
