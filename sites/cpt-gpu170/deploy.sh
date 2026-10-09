#!/usr/bin/env bash
# Deploy the notebook platform onto cpt-gpu170's existing Kubernetes.
#
# Replaces scripts/01-04 and 07-08 for this host -- do NOT run those here:
#   01/mig-setup.sh  would destroy the GPU Operator's MIG layout on all 8 GPUs
#   02 (ufw)         deny-all inbound on a control-plane node running Calico/WEKA
#   03 (k3s)         a second Kubernetes on ports kubeadm already holds
#   07/08 (XFS)      k3s-only; WEKA enforces volume sizes itself
# Reuses scripts/05 (hub) and scripts/06 (tunnel) with this site's overlay.
#
#   sudo -E sites/cpt-gpu170/build-images.sh   # once, and after image changes
#   sudo -E sites/cpt-gpu170/deploy.sh
#   sudo -E sites/cpt-gpu170/verify.sh
#   sudo    sites/cpt-gpu170/gpu-usage.sh      # who holds which slice
#
# Run as root: the only kubeconfig on this host is the root-owned admin.conf.
set -euo pipefail
cd "$(dirname "$0")/../.."
[ -f .env ] && set -a && . ./.env && set +a
export KUBECONFIG="${KUBECONFIG:-/etc/kubernetes/admin.conf}"
SITE=sites/cpt-gpu170
CHART_VERSION="${CHART_VERSION:-4.4.2}"

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

echo "==> pre-flight"
fail=0
need() { if eval "$2"; then echo "  ok    $1"; else echo "  FAIL  $1" >&2; fail=1; fi; }
node=$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')
# jq, not jsonpath: the dot in "mig-1g.35gb" would need escaping too.
alloc() { kubectl get node "$node" -o json | jq -r --arg k "nvidia.com/$1" '.status.allocatable[$k] // empty'; }
need "single node (local images, pullPolicy: Never)" \
     '[ "$(kubectl get nodes --no-headers | wc -l)" -eq 1 ]'
need "nvidia.com/mig-1g.35gb advertised ($(alloc mig-1g.35gb))" '[ "$(alloc mig-1g.35gb)" -ge 1 ] 2>/dev/null'
need "nvidia.com/mig-2g.35gb advertised ($(alloc mig-2g.35gb))" '[ "$(alloc mig-2g.35gb)" -ge 1 ] 2>/dev/null'
need "RuntimeClass nvidia"            'kubectl get runtimeclass nvidia >/dev/null 2>&1'
need "StorageClass wekafs-dir"        'kubectl get storageclass wekafs-dir >/dev/null 2>&1'
need "hub image imported"             "ctr -n k8s.io images ls -q | grep -q 'local/k8s-hub-native:${CHART_VERSION}\$'"
need "singleuser image imported"      "ctr -n k8s.io images ls -q | grep -q 'local/pytorch-caimex:cuda12\$'"
need "no k3s on this host"            '! command -v k3s >/dev/null'
[ "$fail" = 0 ] || { echo "pre-flight failed (images missing? run $SITE/build-images.sh)" >&2; exit 1; }

echo "==> namespace, Pod Security, RBAC"
kubectl create namespace jupyterhub --dry-run=client -o yaml | kubectl apply -f -
# Baseline is enforced: no privileged pods, hostPath, host network or added
# capabilities in the namespace where user code runs -- a backstop if a values
# change ever reintroduces one. Restricted is reported but not enforced; the
# chart's own pods do not all meet it.
kubectl label namespace jupyterhub --overwrite \
    pod-security.kubernetes.io/enforce=baseline \
    pod-security.kubernetes.io/warn=restricted \
    pod-security.kubernetes.io/audit=restricted
kubectl apply -f "$SITE/hub-rbac.yaml"

echo "==> shared WEKA volumes"
kubectl apply -f "$SITE/shared-pvcs.yaml"

echo "==> usage meter"
# Before the hub: the hub reads METER_TOKEN from this secret and will not start
# without it. Created once and never rotated here, so redeploys keep working;
# delete the secret and redeploy to rotate.
if ! kubectl -n jupyterhub get secret zai-meter >/dev/null 2>&1; then
    kubectl -n jupyterhub create secret generic zai-meter \
        --from-literal=token="$(head -c 32 /dev/urandom | base64 | tr -d '/+=\n')"
fi
kubectl -n jupyterhub create configmap zai-meter-app \
    --from-file=meter.py="$SITE/meter/meter.py" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n jupyterhub create configmap zai-meter-config \
    --from-file=config.json="$SITE/meter/config.json" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$SITE/meter.yaml"
# A new meter.py only takes effect on restart (the config is re-read live).
# Stamping its hash on the pod template restarts the meter exactly when the
# code changed, and never otherwise.
app_sha=$(sha256sum "$SITE/meter/meter.py" | cut -c1-16)
kubectl -n jupyterhub patch deploy/zai-meter --type merge -p \
    "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"zai/app-sha\":\"$app_sha\"}}}}}" >/dev/null
kubectl -n jupyterhub rollout status deploy/zai-meter --timeout=180s

echo "==> Caimex account provisioning"
# Optional, and before the hub, which reads these at start. Same rule as the
# other secrets: a redeploy without the values keeps what is already there.
if [ -n "${CAIMEX_SERVICE_PASSWORD:-}" ]; then
    kubectl -n jupyterhub create secret generic caimex-provision \
        --from-literal=api-url="${CAIMEX_API_URL:-}" \
        --from-literal=org-id="${CAIMEX_ORG_ID:-}" \
        --from-literal=email="${CAIMEX_SERVICE_EMAIL:-}" \
        --from-literal=password="$CAIMEX_SERVICE_PASSWORD" --dry-run=client -o yaml | kubectl apply -f -
elif kubectl -n jupyterhub get secret caimex-provision >/dev/null 2>&1; then
    echo "note: CAIMEX_SERVICE_PASSWORD unset - keeping the existing caimex-provision secret"
else
    echo "note: CAIMEX_SERVICE_PASSWORD unset - approved users will not get Caimex accounts"
fi

echo "==> JupyterHub"
KUBECONFIG="$KUBECONFIG" VALUES_OVERLAY="$SITE/values.yaml" SHARED_HOSTPATH=0 \
    scripts/05-jupyterhub.sh

echo "==> admin account"
# Before the tunnel, so `admin` is never reachable while unclaimed.
"$SITE/claim-admin.sh" "${ADMIN_EMAIL:-}"

if [ -n "${TUNNEL_TOKEN:-}" ]; then
    echo "==> Cloudflare tunnel"
    KUBECONFIG="$KUBECONFIG" scripts/06-tunnel.sh
else
    echo "note: TUNNEL_TOKEN unset - skipping the tunnel; the hub is reachable only by port-forward"
fi
