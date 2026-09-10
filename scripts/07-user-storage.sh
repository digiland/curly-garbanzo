#!/usr/bin/env bash
# Give per-user home directories a REAL, enforced size limit.
#
# Why this exists
# ---------------
# k3s's local-path provisioner is hostPath underneath: it records a PVC's
# requested size and enforces nothing. A user granted "10Gi" can write until the
# root filesystem is full, which takes the cluster down with it. The requested
# size is advisory right up until the moment it matters.
#
# Why a loop device
# -----------------
# Real enforcement needs filesystem quotas. The root filesystem here is ext4
# without the `quota`/`project` features enabled, and turning them on means
# tune2fs plus a remount of `/` -- not something to do to a live box. XFS project
# quotas give exactly the semantics we want (a cap on a directory tree, inherited
# by everything created inside it), and an XFS filesystem in a loop-mounted image
# can be created alongside the running root filesystem with no reboot.
#
# The image is fallocated, NOT sparse: a sparse image can be "full" from XFS's
# point of view while the underlying ext4 has run out of blocks, which surfaces
# as I/O errors instead of a clean ENOSPC. Reserving the space up front means the
# quota is the only thing that can stop a write.
set -euo pipefail

IMG="${USER_STORAGE_IMG:-/var/lib/k3s-user-storage.img}"
MNT="${USER_STORAGE_MNT:-/var/lib/k3s-user-storage}"
SIZE="${USER_STORAGE_SIZE:-150G}"
OLD_PATH="/var/lib/rancher/k3s/storage"

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

echo "==> pre-flight"
command -v mkfs.xfs  >/dev/null || { echo "mkfs.xfs missing: apt install xfsprogs" >&2; exit 1; }
command -v xfs_quota >/dev/null || { echo "xfs_quota missing: apt install xfsprogs" >&2; exit 1; }

avail_kb=$(df --output=avail -k / | tail -1)
want_kb=$(numfmt --from=iec "${SIZE}" | awk '{print int($1/1024)}')
if [ ! -f "$IMG" ] && [ "$avail_kb" -lt $((want_kb + 20*1024*1024)) ]; then
    echo "refusing: need ${SIZE} for the image plus 20G headroom on /" >&2
    echo "available: $(df -h --output=avail / | tail -1)" >&2
    exit 1
fi

echo "==> creating ${SIZE} XFS image at ${IMG}"
if [ ! -f "$IMG" ]; then
    fallocate -l "$SIZE" "$IMG"
    mkfs.xfs -q "$IMG"
    echo "    created"
else
    echo "    already exists, leaving it alone"
fi

echo "==> mounting at ${MNT} with prjquota"
mkdir -p "$MNT"
if ! mountpoint -q "$MNT"; then
    mount -o loop,prjquota "$IMG" "$MNT"
fi
# nofail: a failure to mount user storage must not stop the box from booting.
if ! grep -qF "$MNT" /etc/fstab; then
    printf '%s %s xfs loop,prjquota,nofail 0 0\n' "$IMG" "$MNT" >> /etc/fstab
    echo "    added to /etc/fstab"
fi
mountpoint -q "$MNT" || { echo "mount failed" >&2; exit 1; }

echo "==> migrating existing volumes"
# Done while k3s is still up but before it is repointed; volumes are small
# (home directories, not datasets) so this is a copy, not a sync window.
if [ -d "$OLD_PATH" ] && [ -n "$(ls -A "$OLD_PATH" 2>/dev/null)" ]; then
    if [ -z "$(ls -A "$MNT" 2>/dev/null)" ]; then
        cp -a "$OLD_PATH"/. "$MNT"/
        echo "    copied $(ls -1 "$MNT" | wc -l) volume(s); original left at ${OLD_PATH} as a fallback"
    else
        echo "    destination not empty, skipping (already migrated)"
    fi
else
    echo "    nothing to migrate"
fi

echo "==> pointing k3s local-path at ${MNT}"
# config.yaml rather than editing the systemd unit: k3s merges this on every
# start, and the addon's ConfigMap is regenerated from it. Editing the ConfigMap
# directly gets reverted the next time the addon reconciles.
mkdir -p /etc/rancher/k3s
if [ -f /etc/rancher/k3s/config.yaml ]; then
    grep -q 'default-local-storage-path' /etc/rancher/k3s/config.yaml \
      || printf 'default-local-storage-path: "%s"\n' "$MNT" >> /etc/rancher/k3s/config.yaml
else
    printf 'default-local-storage-path: "%s"\n' "$MNT" > /etc/rancher/k3s/config.yaml
fi

echo "==> restarting k3s (running notebooks will be stopped)"
systemctl restart k3s
for i in $(seq 1 60); do
    k3s kubectl get --raw='/readyz' >/dev/null 2>&1 && break
    sleep 5
done
k3s kubectl get --raw='/readyz' >/dev/null 2>&1 || { echo "k3s did not come back" >&2; exit 1; }

echo "==> verifying the provisioner picked it up"
k3s kubectl -n kube-system get cm local-path-config -o jsonpath='{.data.config\.json}' | grep -q "$MNT" \
  && echo "    local-path now provisions into ${MNT}" \
  || echo "    WARNING: ConfigMap still shows the old path; check k3s logs"

echo
echo "Done. Quotas are applied by scripts/08-quota-apply.sh -- install its timer:"
echo "  sudo scripts/08-quota-apply.sh --install-timer"
