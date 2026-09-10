#!/usr/bin/env bash
# Recreate the 7 x 1g.18gb MIG layout. Idempotent: safe to run repeatedly.
# MIG *mode* survives reboot; the GPU/compute instance layout does not.
set -uo pipefail

PROFILE=19          # MIG 1g.18gb
WANT=7

command -v nvidia-smi >/dev/null || { echo "nvidia-smi absent"; exit 1; }

# At boot this may run before the driver/device nodes are ready. Wait, don't fail.
for i in $(seq 1 60); do
    nvidia-smi -L >/dev/null 2>&1 && break
    [ "$i" -eq 60 ] && { echo "driver not ready after 120s"; exit 1; }
    sleep 2
done

# Ensure MIG mode is on (persists across reboot, but re-assert defensively).
if ! nvidia-smi -i 0 --query-gpu=mig.mode.current --format=csv,noheader | grep -q Enabled; then
    echo "MIG mode off; enabling"
    nvidia-smi -i 0 -mig 1 || exit 1
fi

# Already in the desired shape? Do nothing.
if [ "$(nvidia-smi -L 2>/dev/null | grep -c 'MIG 1g.18gb')" -eq "$WANT" ]; then
    echo "MIG layout already correct ($WANT x 1g.18gb)"
    exit 0
fi

echo "Rebuilding MIG layout -> $WANT x 1g.18gb"
nvidia-smi mig -dci  || true      # destroy compute instances first
nvidia-smi mig -dgi  || true      # then GPU instances

LIST=$(printf "%s," $(yes "$PROFILE" | head -n "$WANT") | sed 's/,$//')
nvidia-smi mig -cgi "$LIST" -C || { echo "MIG creation FAILED"; exit 1; }

COUNT=$(nvidia-smi -L | grep -c 'MIG 1g.18gb')
[ "$COUNT" -eq "$WANT" ] || { echo "expected $WANT instances, got $COUNT"; exit 1; }
echo "MIG layout OK: $COUNT x 1g.18gb"
