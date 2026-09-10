#!/usr/bin/env bash
# Deploy cloudflared. Requires TUNNEL_TOKEN (see .env.example).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
[ -n "${TUNNEL_TOKEN:-}" ] || { echo "TUNNEL_TOKEN not set (see .env.example)"; exit 1; }

install -m 0644 host/99-cloudflared-quic.conf /etc/sysctl.d/99-cloudflared-quic.conf
sysctl -p /etc/sysctl.d/99-cloudflared-quic.conf

kubectl -n jupyterhub create secret generic cloudflared-token \
  --from-literal=token="$TUNNEL_TOKEN" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s/cloudflared.yaml
kubectl -n jupyterhub rollout status deploy/cloudflared --timeout=120s

cat <<'MSG'

NEXT: map the hostname in the Cloudflare dashboard.
  Zero Trust -> Networks -> Tunnels -> your tunnel -> Published application

  Subdomain/Domain : e.g. notebooks / example.com
  Type             : HTTP     <-- NOT HTTPS. Cloudflare terminates TLS at the edge;
                                  choosing HTTPS here is the usual cause of a 502.
  URL              : http://proxy-public.jupyterhub.svc.cluster.local:80
MSG
