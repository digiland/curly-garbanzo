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
alloc() { kubectl get node "$node" -o jsonpath="{.status.allocatable.nvidia\.com/$1}"; }
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

echo "==> shared WEKA volumes"
kubectl create namespace jupyterhub --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$SITE/shared-pvcs.yaml"

echo "==> JupyterHub"
KUBECONFIG="$KUBECONFIG" VALUES_OVERLAY="$SITE/values.yaml" SHARED_HOSTPATH=0 \
    scripts/05-jupyterhub.sh

if [ -n "${TUNNEL_TOKEN:-}" ]; then
    echo "==> Cloudflare tunnel"
    KUBECONFIG="$KUBECONFIG" scripts/06-tunnel.sh
else
    echo "note: TUNNEL_TOKEN unset - skipping the tunnel; the hub is reachable only by port-forward"
fi
