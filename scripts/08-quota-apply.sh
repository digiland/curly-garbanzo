#!/usr/bin/env bash
# Apply an XFS project quota to every user volume, sized from its PVC request.
#
# Runs as a reconciler on a timer rather than as a provisioner hook. The
# local-path `setup` script would be the obvious place, but it executes inside a
# busybox helper pod that has neither xfsprogs nor a view of the host mount --
# and a reconciler is idempotent, so it also repairs volumes created while it
# was not running, and picks up a PVC whose size was edited after creation.
#
# Project quotas are inherited: once a directory is assigned to a project, every
# file created beneath it counts against that project's limit. A user who hits
# the cap gets a normal ENOSPC on write and can delete files to recover, which
# is the behaviour people expect from a disk being full.
set -euo pipefail

MNT="${USER_STORAGE_MNT:-/var/lib/k3s-user-storage}"
PROJID_FILE=/etc/projid
PROJECTS_FILE=/etc/projects
ALARM_PCT="${DISK_ALARM_PCT:-85}"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

install_timer() {
    local self; self="$(readlink -f "$0")"
    cat > /etc/systemd/system/jupyter-quota.service <<EOF
[Unit]
Description=Apply XFS project quotas to JupyterHub user volumes
After=k3s.service
[Service]
Type=oneshot
ExecStart=${self}
EOF
    cat > /etc/systemd/system/jupyter-quota.timer <<'EOF'
[Unit]
Description=Reconcile JupyterHub user volume quotas
[Timer]
# Frequent enough that a new user is capped within a couple of minutes of their
# first login, which is well before they could fill a disk.
OnBootSec=2min
OnUnitActiveSec=2min
[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    systemctl enable --now jupyter-quota.timer
    echo "installed: jupyter-quota.timer (every 2 min)"
    systemctl list-timers jupyter-quota.timer --no-pager || true
}

[ "${1:-}" = "--install-timer" ] && { install_timer; exit 0; }

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
mountpoint -q "$MNT" || { echo "$MNT is not mounted; run scripts/07-user-storage.sh" >&2; exit 1; }

touch "$PROJID_FILE" "$PROJECTS_FILE"

# Stable project id per volume. Reusing an id across two live directories would
# merge their quotas, so ids are allocated once and never recycled.
projid_for() {
    local name="$1" id
    id=$(awk -F: -v n="$name" '$1==n {print $2}' "$PROJID_FILE")
    if [ -z "$id" ]; then
        id=$(awk -F: 'BEGIN{m=100} $2>m {m=$2} END{print m+1}' "$PROJID_FILE")
        printf '%s:%s\n' "$name" "$id" >> "$PROJID_FILE"
    fi
    echo "$id"
}

applied=0
for dir in "$MNT"/*/; do
    [ -d "$dir" ] || continue
    dir="${dir%/}"
    name="$(basename "$dir")"
    pv="${name%%_*}"                                  # pvc-<uuid>_<ns>_<claim>
    [ "${pv#pvc-}" != "$pv" ] || continue             # not a provisioned volume

    size=$(kubectl get pv "$pv" -o jsonpath='{.spec.capacity.storage}' 2>/dev/null || true)
    [ -n "$size" ] || { echo "skip $name: no PV capacity found"; continue; }

    # Ki/Mi/Gi -> the k/m/g suffixes xfs_quota understands (both are binary).
    limit=$(echo "$size" | sed -E 's/([0-9]+)Ki?$/\1k/; s/([0-9]+)Mi?$/\1m/; s/([0-9]+)Gi?$/\1g/; s/([0-9]+)Ti?$/\1t/')

    id=$(projid_for "$name")
    grep -q "^${id}:${dir}$" "$PROJECTS_FILE" || printf '%s:%s\n' "$id" "$dir" >> "$PROJECTS_FILE"

    xfs_quota -x -c "project -s -p ${dir} ${id}" "$MNT" >/dev/null 2>&1 || true
    xfs_quota -x -c "limit -p bhard=${limit} ${id}" "$MNT" >/dev/null 2>&1 || {
        echo "FAILED to set quota on $name"; continue; }
    applied=$((applied + 1))
done
echo "quotas applied to ${applied} volume(s)"

# The quotas above cap each user, but the image itself is a fixed size: if the
# sum of what users actually store approaches it, new writes start failing for
# everyone. Surface that before it happens.
used_pct=$(df --output=pcent "$MNT" | tail -1 | tr -dc '0-9')
if [ "${used_pct:-0}" -ge "$ALARM_PCT" ]; then
    msg="user storage at ${used_pct}% of $(df -h --output=size "$MNT" | tail -1 | tr -d ' ')"
    echo "ALARM: $msg" >&2
    logger -t jupyter-quota -p user.warning "$msg" 2>/dev/null || true
fi
