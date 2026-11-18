#!/usr/bin/env bash
# Disk guard: stop a user's notebook server before their home volume fills the
# node's root disk.
#
# Why this exists
# ---------------
# On 2026-11-11 two home volumes (217G + 188G, one a training run writing
# checkpoints) took / to 94%. The kubelet evicted EVERY pod (hub, proxy,
# cloudflared, serverless gateway), so the whole site went down, not just the
# notebook doing the writing. local-path volumes are hostPath: their requested
# size is never enforced. scripts/07/08 add real XFS quotas, but the data no
# longer fits their 150G image, so until that migration happens this guard is
# the safety net.
#
# Policy (checked every minute by a systemd timer)
#   1. Per-user cap (CAP_GB, default 100). A user over the cap whose volume is
#      STILL GROWING has their server stopped. A user over the cap who is not
#      growing is left alone, so they can start a server and delete files to get
#      back under it.
#   2. Whole disk. At WARN_PCT (85) used it logs. At STOP_PCT (90) it stops the
#      running server whose volume grew most over the last 10 minutes, one per
#      run, until the disk is back under STOP_PCT. That keeps it well above the
#      kubelet's 5%-free eviction line.
# A stop is a pod delete; KubeSpawner sees the pod gone and marks the server
# stopped. Files are never touched. Every action goes to syslog (tag
# zai-disk-guard) and /var/log/zai-disk-guard.log.
set -euo pipefail

STORAGE="${STORAGE_PATH:-/var/lib/rancher/k3s/storage}"
NS="${HUB_NAMESPACE:-jupyterhub}"
CAP_GB="${CAP_GB:-100}"
WARN_PCT="${WARN_PCT:-85}"
STOP_PCT="${STOP_PCT:-90}"
GROW_TOLERANCE_MB="${GROW_TOLERANCE_MB:-256}"   # ignore jitter (logs, caches)
EXEMPT="${EXEMPT:-}"                            # space-separated claim names
STATE=/var/lib/zai-disk-guard
LOG=/var/log/zai-disk-guard.log
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

install_timer() {
    local self; self="$(readlink -f "$0")"
    cat > /etc/systemd/system/zai-disk-guard.service <<EOF
[Unit]
Description=Stop notebook servers that are filling the root disk
After=k3s.service
[Service]
Type=oneshot
Environment=CAP_GB=${CAP_GB}
ExecStart=${self}
EOF
    cat > /etc/systemd/system/zai-disk-guard.timer <<'EOF'
[Unit]
Description=Run the ZAI disk guard every minute
[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=10s
[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    systemctl enable --now zai-disk-guard.timer
    echo "installed: zai-disk-guard.timer (every minute, cap ${CAP_GB}G)"
}

[ "${1:-}" = "--install-timer" ] && { install_timer; exit 0; }
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
mkdir -p "$STATE/history"

log() {
    local msg="$(date -u +%FT%TZ) $*"
    echo "$msg" | tee -a "$LOG"
    logger -t zai-disk-guard -- "$*" 2>/dev/null || true
}

now=$(date +%s)
used_pct=$(df --output=pcent / | tail -1 | tr -dc 0-9)
cap_kb=$((CAP_GB * 1024 * 1024))
tol_kb=$((GROW_TOLERANCE_MB * 1024))

# Running pods that mount each claim (claim -> pod). Only Running pods can grow.
declare -A POD=()
while read -r pod claim; do
    [ -n "$claim" ] && POD[$claim]=$pod
done < <(kubectl -n "$NS" get pods --field-selector=status.phase=Running -o \
    jsonpath='{range .items[*]}{.metadata.name}{" "}{range .spec.volumes[*]}{.persistentVolumeClaim.claimName}{" "}{end}{"\n"}{end}' 2>/dev/null \
    | awk '{for(i=2;i<=NF;i++) print $1, $i}')

stop() {   # claim reason
    local claim="$1" pod="${POD[$1]:-}"
    [ -n "$pod" ] || return 1
    if [ "$DRY" = 1 ]; then log "DRY-RUN would stop $pod ($claim): $2"; return 0; fi
    log "STOP $pod ($claim): $2"
    kubectl -n "$NS" delete pod "$pod" --wait=false >/dev/null 2>&1 \
        || log "  delete of $pod failed"
    kubectl -n "$NS" annotate pvc "$claim" --overwrite \
        zai.dev/disk-guard="stopped $(date -u +%FT%TZ): $2" >/dev/null 2>&1 || true
    unset "POD[$claim]"
}

# Measure every user volume; keep 15 minutes of history per volume.
declare -A SIZE=() PREV=() GROW10=()
for dir in "$STORAGE"/pvc-*_"${NS}"_claim-*; do
    [ -d "$dir" ] || continue
    claim="${dir##*_}"
    case " $EXEMPT " in *" $claim "*) continue ;; esac
    kb=$(du -sk "$dir" 2>/dev/null | cut -f1) || continue
    SIZE[$claim]=$kb
    hist="$STATE/history/$claim"
    touch "$hist"                                  # first run: empty history
    PREV[$claim]=$(tail -1 "$hist" 2>/dev/null | cut -d' ' -f2)
    old=$(awk -v t=$((now - 600)) '$1<=t {v=$2} END{print v}' "$hist" 2>/dev/null)
    [ -n "$old" ] || old=$(head -1 "$hist" 2>/dev/null | cut -d' ' -f2)
    GROW10[$claim]=$(( kb - ${old:-$kb} ))
    { awk -v t=$((now - 900)) '$1>t' "$hist" 2>/dev/null; echo "$now $kb"; } > "$hist.tmp"
    mv "$hist.tmp" "$hist"
done

# 1. Per-user cap: stop only users over the cap who are still writing.
for claim in "${!SIZE[@]}"; do
    kb=${SIZE[$claim]}; prev=${PREV[$claim]:-$kb}
    [ "$kb" -gt "$cap_kb" ] || continue
    if [ $((kb - prev)) -gt "$tol_kb" ] && [ -n "${POD[$claim]:-}" ]; then
        stop "$claim" "$((kb/1048576))G is over the ${CAP_GB}G cap and grew $(((kb-prev)/1024))M in the last minute"
    fi
done

# 2. Whole disk.
if [ "$used_pct" -ge "$STOP_PCT" ]; then
    best=""; best_g=0
    for claim in "${!POD[@]}"; do
        g=${GROW10[$claim]:-0}
        [ -n "${SIZE[$claim]:-}" ] && [ "$g" -gt "$best_g" ] && { best=$claim; best_g=$g; }
    done
    if [ -n "$best" ]; then
        stop "$best" "root disk ${used_pct}% used (>= ${STOP_PCT}%); fastest-growing volume (+$((best_g/1024))M in 10 min)"
    else
        log "ALERT root disk ${used_pct}% used, but no running user server is growing; growth is elsewhere"
    fi
elif [ "$used_pct" -ge "$WARN_PCT" ]; then
    # Once every 30 minutes is enough to see in the log without flooding it.
    last=$(cat "$STATE/last-warn" 2>/dev/null || echo 0)
    if [ $((now - last)) -ge 1800 ]; then
        top=$(for c in "${!SIZE[@]}"; do echo "${SIZE[$c]} $c"; done | sort -rn | head -3 \
              | awk '{printf "%s=%dG ", $2, $1/1048576}')
        log "WARN root disk ${used_pct}% used; largest: ${top}"
        echo "$now" > "$STATE/last-warn"
    fi
fi
