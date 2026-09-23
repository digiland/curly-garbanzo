#!/usr/bin/env bash
# Build the hub and singleuser images on cpt-gpu170, which has no Docker.
#
# scripts/04-images.sh needs `docker build`, then imports into k3s's containerd.
# Neither exists here, and the only registries in use are Rafay's pull mirrors,
# so there is nowhere to push. Instead:
#
#   1. a one-shot BuildKit pod (daemonless, privileged) builds both Dockerfiles
#      straight from this checkout and writes docker-format tarballs to /local
#   2. the tarballs are imported into containerd's k8s.io namespace -- the one
#      kubelet reads -- under the same names the base values use, so
#      pullPolicy: Never finds them
#
# This works because the cluster is ONE node: the image exists only where it was
# imported. Adding a node means pushing to a real registry instead (see
# scripts/04-images.sh REGISTRY=).
#
# Run as root (reads /etc/kubernetes/admin.conf, writes containerd).
set -euo pipefail
cd "$(dirname "$0")/../.."
export KUBECONFIG="${KUBECONFIG:-/etc/kubernetes/admin.conf}"

CHART_VERSION="${CHART_VERSION:-4.4.2}"
BUILDKIT_IMAGE="${BUILDKIT_IMAGE:-docker.io/moby/buildkit:v0.23.2}"
# /local is the 14 TB NVMe scratch volume; the singleuser tarball is >10 GB and
# / has well under 100 GB free.
OUT="${BUILD_OUT:-/local/zai-build}"
NS=jupyterhub
JOB=zai-image-build
SRC="$(pwd)"
NODE="$(hostname)"

HUB_TAG="docker.io/local/k8s-hub-native:${CHART_VERSION}"
SU_TAG="docker.io/local/pytorch-caimex:cuda12"

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

mkdir -p "$OUT"
rm -f "$OUT"/hub.tar "$OUT"/singleuser.tar
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" delete job "$JOB" --ignore-not-found --wait=true

echo "==> building in-cluster with ${BUILDKIT_IMAGE}"
kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB}
  namespace: ${NS}
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 7200
  ttlSecondsAfterFinished: 3600
  template:
    spec:
      restartPolicy: Never
      nodeName: ${NODE}              # the tarballs land on this node's /local
      containers:
      - name: buildkit
        image: ${BUILDKIT_IMAGE}
        securityContext: {privileged: true}
        command: ["sh", "-euc"]
        args:
        - |
          build() {  # <context dir> <image name> <tarball>
            buildctl-daemonless.sh build \
              --frontend dockerfile.v0 \
              --local context=/src/\$1 --local dockerfile=/src/\$1 \
              --output type=docker,name=\$2,dest=/out/\$3.partial
            mv /out/\$3.partial /out/\$3
          }
          build images/hub        ${HUB_TAG} hub.tar
          build images/singleuser ${SU_TAG}  singleuser.tar
        volumeMounts:
        - {name: src, mountPath: /src, readOnly: true}
        - {name: out, mountPath: /out}
        - {name: state, mountPath: /var/lib/buildkit}   # rootful state dir
      volumes:
      - name: src
        hostPath: {path: ${SRC}, type: Directory}
      - name: out
        hostPath: {path: ${OUT}, type: Directory}
      - name: state
        emptyDir: {}
EOF

# Stream the build log; the singleuser image takes a while (CUDA base + pip).
for _ in $(seq 1 60); do
    kubectl -n "$NS" get pods -l job-name="$JOB" -o jsonpath='{.items[0].status.phase}' 2>/dev/null \
      | grep -qE 'Running|Succeeded|Failed' && break
    sleep 5
done
kubectl -n "$NS" logs -f job/"$JOB" || true
kubectl -n "$NS" wait --for=condition=complete job/"$JOB" --timeout=60s \
  || { echo "BUILD FAILED - see: kubectl -n $NS logs job/$JOB" >&2; exit 1; }

echo "==> importing into containerd (k8s.io namespace)"
ctr -n k8s.io images import "$OUT/hub.tar"
ctr -n k8s.io images import "$OUT/singleuser.tar"
rm -f "$OUT/hub.tar" "$OUT/singleuser.tar"
kubectl -n "$NS" delete job "$JOB" --ignore-not-found

ctr -n k8s.io images ls -q | grep -E 'local/(k8s-hub-native|pytorch-caimex)'
