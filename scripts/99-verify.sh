#!/usr/bin/env bash
# Health check. Run after deploy, and after any reboot.
set -uo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
EXPECT="${MIG_COUNT:-7}"
fail=0
chk() { printf '  %-42s %s\n' "$1" "$2"; [ "$3" = ok ] || fail=1; }

# On one node this is the local MIG layout. On a cluster nvidia-smi only sees
# this host, so the local check is skipped and the cluster-wide allocatable
# total is what matters -- `.items[0]` would report one node's slices as if they
# were the whole cluster.
NODES=$(kubectl get nodes --no-headers 2>/dev/null | wc -l)

if [ "${NODES:-1}" -le 1 ]; then
    n=$(nvidia-smi -L 2>/dev/null | grep -c 'MIG ' || echo 0)
    chk "MIG instances" "$n / $EXPECT" "$([ "$n" = "$EXPECT" ] && echo ok || echo bad)"
else
    printf '  %-42s %s\n' "cluster nodes" "$NODES"
fi

# Sum over every node, not just the first.
a=$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' 2>/dev/null \
    | awk '{s+=$1} END{print s+0}')
if [ "${NODES:-1}" -le 1 ]; then
    chk "nvidia.com/gpu allocatable" "${a:-0} / $EXPECT" "$([ "${a:-0}" = "$EXPECT" ] && echo ok || echo bad)"
else
    chk "nvidia.com/gpu allocatable (cluster)" "${a:-0}" "$([ "${a:-0}" -ge 1 ] && echo ok || echo bad)"

    # Every node must carry a pool label: an unlabelled node matches no profile
    # in the cluster overlay and silently accepts no user servers at all.
    un=$(kubectl get nodes -L caimex.io/pool --no-headers 2>/dev/null | awk '$NF==""||NF<6{c++} END{print c+0}')
    chk "nodes labelled caimex.io/pool" "$((NODES-un)) / $NODES" "$([ "${un:-0}" -eq 0 ] && echo ok || echo bad)"

    # pullPolicy: Never is correct on one node and fatal on many -- a pod on a
    # node that never imported the image does not even attempt a pull.
    pp=$(kubectl -n jupyterhub get deploy hub -o jsonpath='{.spec.template.spec.containers[0].imagePullPolicy}' 2>/dev/null)
    chk "hub imagePullPolicy" "${pp:-?}" "$([ "$pp" != "Never" ] && echo ok || echo bad)"

    # local-path is hostPath underneath: it pins each user to one node and
    # serves a different empty directory for ~/team on every other node.
    sc=$(kubectl -n jupyterhub get pvc jupyter-shared-team -o jsonpath='{.spec.storageClassName}' 2>/dev/null)
    am=$(kubectl -n jupyterhub get pvc jupyter-shared-team -o jsonpath='{.spec.accessModes[0]}' 2>/dev/null)
    chk "shared storage is RWX" "${sc:-none} ${am:-}" "$([ "$am" = "ReadWriteMany" ] && echo ok || echo bad)"
fi

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

# The admin account is claimed only if NativeAuthenticator has a credential row
# for it. JupyterHub auto-creates a `users` row for everything in admin_users, so
# checking there would report "claimed" for an account anyone can still register.
adm=$(kubectl -n jupyterhub exec deploy/hub -- python -c "
import sqlite3
c=sqlite3.connect('/srv/jupyterhub/jupyterhub.sqlite')
print(sum(1 for _ in c.execute(\"select 1 from users_info where username='admin'\")))
" 2>/dev/null || echo 0)
chk "admin account claimed" "$([ "${adm:-0}" -ge 1 ] && echo yes || echo 'NO - anyone can register it')" \
    "$([ "${adm:-0}" -ge 1 ] && echo ok || echo bad)"

# Culling is what returns MIG slices to the pool. It fails silently: a role
# misconfiguration turns every poll into a 403 and idle servers keep their slice.
f=$(kubectl -n jupyterhub logs deploy/hub --tail=500 2>/dev/null | grep -c "403: Forbidden" || true)
chk "idle culler authorised" "$f 403s in last 500 lines" "$([ "${f:-0}" -eq 0 ] && echo ok || echo bad)"

# Quota enforcement. Without the XFS mount, PVC sizes are advisory and a single
# user can fill the root filesystem.
if mountpoint -q "${USER_STORAGE_MNT:-/var/lib/k3s-user-storage}" 2>/dev/null; then
    chk "user storage quota-enforced" "mounted" ok
    t=$(systemctl is-active jupyter-quota.timer 2>/dev/null || echo inactive)
    chk "quota reconcile timer" "$t" "$([ "$t" = active ] && echo ok || echo bad)"
    d=$(df --output=pcent "${USER_STORAGE_MNT:-/var/lib/k3s-user-storage}" 2>/dev/null | tail -1 | tr -dc '0-9')
    chk "user storage used" "${d:-?}%" "$([ "${d:-0}" -lt 85 ] && echo ok || echo bad)"
elif [ "${NODES:-1}" -le 1 ]; then
    chk "user storage quota-enforced" "NOT mounted - quotas unenforced" bad
else
    # scripts/07 is per-node and is superseded on a cluster: quotas come from
    # the RWX backend's own limits and the PVC size, not a local XFS image.
    printf '  %-42s %s\n' "user storage quota" "delegated to RWX backend"
fi

r=$(df --output=pcent / | tail -1 | tr -dc '0-9')
chk "root filesystem used" "${r}%" "$([ "$r" -lt 85 ] && echo ok || echo bad)"

u=$(kubectl -n jupyterhub get pods -l component=singleuser-server --no-headers 2>/dev/null | wc -l)
printf '  %-42s %s\n' "user servers running" "$u / $EXPECT"
echo
[ "$fail" = 0 ] && echo "ALL CHECKS PASSED" || { echo "SOME CHECKS FAILED"; exit 1; }
