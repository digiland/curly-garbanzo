#!/usr/bin/env bash
# Health check for cpt-gpu170. The generic scripts/99-verify.sh checks the k3s
# layout (7 x nvidia.com/gpu, its own device plugin, the XFS quota mount), all of
# which are absent here by design and would report failures that are not.
set -uo pipefail
export KUBECONFIG="${KUBECONFIG:-/etc/kubernetes/admin.conf}"
fail=0
chk() { printf '  %-44s %s\n' "$1" "$2"; [ "$3" = ok ] || fail=1; }

node=$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')
for r in mig-1g.35gb mig-2g.35gb; do
    a=$(kubectl get node "$node" -o jsonpath="{.status.allocatable.nvidia\.com/$r}")
    chk "nvidia.com/$r allocatable" "${a:-0}" "$([ "${a:-0}" -ge 1 ] 2>/dev/null && echo ok || echo bad)"
done

# MIG is owned by the GPU Operator here; a failed state means it is mid-change
# or has given up, and spawns will stall.
st=$(kubectl get node "$node" -o jsonpath='{.metadata.labels.nvidia\.com/mig\.config\.state}')
chk "GPU Operator MIG config state" "${st:-?}" "$([ "$st" = success ] && echo ok || echo bad)"

p=$(kubectl -n gpu-operator-resources get pods -l app=nvidia-device-plugin-daemonset --no-headers 2>/dev/null | grep -c Running)
chk "device plugin running" "$p" "$([ "${p:-0}" -ge 1 ] && echo ok || echo bad)"

for pvc in jupyter-shared-datasets jupyter-shared-team; do
    # WaitForFirstConsumer: Pending is normal until the first server mounts it.
    s=$(kubectl -n jupyterhub get pvc "$pvc" -o jsonpath='{.status.phase}' 2>/dev/null)
    chk "pvc/$pvc" "${s:-missing}" "$([ "$s" = Bound ] || [ "$s" = Pending ] && echo ok || echo bad)"
done

for d in hub proxy; do
    s=$(kubectl -n jupyterhub get deploy "$d" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
    chk "deploy/$d ready" "${s:-0}" "$([ "${s:-0}" -ge 1 ] && echo ok || echo bad)"
done
if kubectl -n jupyterhub get deploy cloudflared >/dev/null 2>&1; then
    s=$(kubectl -n jupyterhub get deploy cloudflared -o jsonpath='{.status.readyReplicas}')
    chk "deploy/cloudflared ready" "${s:-0}" "$([ "${s:-0}" -ge 1 ] && echo ok || echo bad)"
fi

# CPU-only servers must not be on the nvidia runtime -- see values.yaml.
rc=$(helm -n jupyterhub get values jupyterhub --all -o json 2>/dev/null \
     | python3 -c 'import json,sys; print((json.load(sys.stdin).get("singleuser",{}).get("extraPodConfig") or {}).get("runtimeClassName") or "")' 2>/dev/null)
chk "no default nvidia runtime for all pods" "${rc:-unset}" "$([ -z "$rc" ] && echo ok || echo bad)"

if [ -n "${PUBLIC_HOSTNAME:-}" ]; then
    c=$(curl -s -o /dev/null -w '%{http_code}' "https://${PUBLIC_HOSTNAME}/hub/login")
    chk "https://${PUBLIC_HOSTNAME}/hub/login" "HTTP $c" "$([ "$c" = 200 ] && echo ok || echo bad)"
fi

# See scripts/99-verify.sh: only a users_info row means the account is claimed.
adm=$(kubectl -n jupyterhub exec deploy/hub -- python -c "
import sqlite3
c=sqlite3.connect('/srv/jupyterhub/jupyterhub.sqlite')
print(sum(1 for _ in c.execute(\"select 1 from users_info where username='admin'\")))
" 2>/dev/null || echo 0)
chk "admin account claimed" "$([ "${adm:-0}" -ge 1 ] && echo yes || echo 'NO - anyone can register it')" \
    "$([ "${adm:-0}" -ge 1 ] && echo ok || echo bad)"

f=$(kubectl -n jupyterhub logs deploy/hub --tail=500 2>/dev/null | grep -c "403: Forbidden")
chk "idle culler authorised" "$f 403s in last 500 lines" "$([ "${f:-0}" -eq 0 ] && echo ok || echo bad)"

u=$(kubectl -n jupyterhub get pods -l component=singleuser-server --no-headers 2>/dev/null | wc -l)
printf '  %-44s %s\n' "user servers running" "$u"
echo
[ "$fail" = 0 ] && echo "ALL CHECKS PASSED" || { echo "SOME CHECKS FAILED"; exit 1; }
