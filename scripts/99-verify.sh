#!/usr/bin/env bash
# Health check. Run after deploy, and after any reboot.
set -uo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
EXPECT="${MIG_COUNT:-7}"
fail=0
chk() { printf '  %-42s %s\n' "$1" "$2"; [ "$3" = ok ] || fail=1; }

n=$(nvidia-smi -L 2>/dev/null | grep -c 'MIG ' || echo 0)
chk "MIG instances" "$n / $EXPECT" "$([ "$n" = "$EXPECT" ] && echo ok || echo bad)"

a=$(kubectl get node -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}' 2>/dev/null || echo 0)
chk "nvidia.com/gpu allocatable" "${a:-0} / $EXPECT" "$([ "${a:-0}" = "$EXPECT" ] && echo ok || echo bad)"

for d in hub proxy cloudflared; do
    s=$(kubectl -n jupyterhub get deploy "$d" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
    chk "deploy/$d ready" "${s:-0}" "$([ "${s:-0}" -ge 1 ] 2>/dev/null && echo ok || echo bad)"
done

p=$(kubectl -n nvidia-device-plugin get pods --no-headers 2>/dev/null | grep -c Running || echo 0)
chk "device plugin running" "$p" "$([ "$p" -ge 1 ] && echo ok || echo bad)"

if [ -n "${PUBLIC_HOSTNAME:-}" ]; then
    c=$(curl -s -o /dev/null -w '%{http_code}' "https://${PUBLIC_HOSTNAME}/hub/login" 2>/dev/null)
    chk "https://${PUBLIC_HOSTNAME}/hub/login" "HTTP $c" "$([ "$c" = 200 ] && echo ok || echo bad)"
fi

u=$(kubectl -n jupyterhub get pods -l component=singleuser-server --no-headers 2>/dev/null | wc -l)
printf '  %-42s %s\n' "user servers running" "$u / $EXPECT"
echo
[ "$fail" = 0 ] && echo "ALL CHECKS PASSED" || { echo "SOME CHECKS FAILED"; exit 1; }
