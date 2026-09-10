#!/usr/bin/env bash
# Multi-node prerequisites: node pool labels, the Training Operator, and Kueue.
# Idempotent. Run once per cluster from a control-plane node, AFTER shared RWX
# storage exists (see docs/CLUSTER.md) and BEFORE applying the cluster overlay.
#
# Does NOT touch a running single-node deploy: it only adds labels and two
# namespaces of its own.
set -euo pipefail
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

TRAINING_OPERATOR_VER="${TRAINING_OPERATOR_VER:-v1.8.1}"
KUEUE_VER="${KUEUE_VER:-v0.10.1}"

# --- node pools -------------------------------------------------------------
# Every node is labelled, because an unlabelled node matches no profile in the
# cluster overlay and silently accepts nothing.
#   mig  MIG-partitioned, interactive/dev            (nvidia.com/gpu present, MIG on)
#   gpu  whole GPUs, training                        (nvidia.com/gpu present, MIG off)
#   cpu  no GPU
echo "==> labelling node pools"
for n in $(kubectl get nodes -o jsonpath='{.items[*].metadata.name}'); do
    if [ -n "$(kubectl get node "$n" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}' 2>/dev/null)" ]; then
        # A MIG node reports its slices via the device plugin; the product label
        # from NFD/GFD carries the MIG profile when one is configured.
        if kubectl get node "$n" -o jsonpath='{.metadata.labels}' 2>/dev/null | grep -qi 'mig-'; then
            pool=mig
        else
            pool=gpu
        fi
    else
        pool=cpu
    fi
    kubectl label node "$n" caimex.io/pool="$pool" --overwrite
done
kubectl get nodes -L caimex.io/pool

# --- Kubeflow Training Operator ---------------------------------------------
# Supplies the PyTorchJob CRD: creates master/worker pods and wires up
# MASTER_ADDR, MASTER_PORT, RANK and WORLD_SIZE. A notebook pod cannot do this
# for itself, which is why multi-node training is not a spawn profile.
echo "==> Kubeflow Training Operator ${TRAINING_OPERATOR_VER}"
kubectl apply --server-side -k \
  "github.com/kubeflow/training-operator/manifests/overlays/standalone?ref=${TRAINING_OPERATOR_VER}"

# --- Kueue ------------------------------------------------------------------
echo "==> Kueue ${KUEUE_VER}"
kubectl apply --server-side -f \
  "https://github.com/kubernetes-sigs/kueue/releases/download/${KUEUE_VER}/manifests.yaml"

echo "==> waiting for controllers"
kubectl -n kubeflow            rollout status deploy/training-operator --timeout=300s
kubectl -n kueue-system        rollout status deploy/kueue-controller-manager --timeout=300s

# Quotas reference the CRDs above, so they apply only once the webhooks are up.
echo "==> queues"
kubectl apply -f "$(dirname "$0")/../k8s/training/kueue-quotas.yaml"

cat <<'EOF'

Done. Still required before the cluster overlay will work:
  1. RWX storage class, and k8s/cluster/shared-pvcs.yaml applied
  2. images pushed to a registry:  REGISTRY=ghcr.io/you scripts/04-images.sh
  3. placeholders substituted in charts/jupyterhub-values-cluster.yaml
See docs/CLUSTER.md.
EOF
