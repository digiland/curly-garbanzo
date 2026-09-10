#!/usr/bin/env bash
# Enable MIG and partition the GPU into 7 x 1g.18gb, with boot persistence.
set -euo pipefail
cd "$(dirname "$0")/.."

PROFILE="${MIG_PROFILE:-19}"   # 19 = 1g.18gb on H200 (nvidia-smi mig -lgip to list)
COUNT="${MIG_COUNT:-7}"

command -v nvidia-smi >/dev/null || { echo "nvidia-smi missing - install the driver first"; exit 1; }

if nvidia-smi --query-compute-apps=pid --format=csv,noheader | grep -q .; then
    echo "ERROR: processes are still using the GPU. Stop them before enabling MIG:"
    nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
    exit 1
fi

echo "==> enabling MIG mode"
nvidia-smi -pm 0                       # persistence mode blocks the mode change
nvidia-smi -i 0 -mig 1

if ! nvidia-smi --query-gpu=mig.mode.current --format=csv,noheader | grep -q Enabled; then
    echo "MIG mode is pending a GPU reset. Trying reset; if it fails, REBOOT and re-run."
    nvidia-smi --gpu-reset -i 0 || { echo ">>> reboot required, then re-run this script"; exit 2; }
fi

echo "==> installing boot-persistence unit"
install -m 0755 host/mig-setup.sh /usr/local/bin/mig-setup.sh
install -m 0644 host/nvidia-mig-layout.service /etc/systemd/system/nvidia-mig-layout.service
systemctl daemon-reload
systemctl enable nvidia-mig-layout.service

echo "==> building layout (${COUNT} x profile ${PROFILE})"
/usr/local/bin/mig-setup.sh
nvidia-smi -pm 1

nvidia-smi -L
n=$(nvidia-smi -L | grep -c 'MIG ' || true)
[ "$n" -eq "$COUNT" ] || { echo "expected $COUNT MIG devices, got $n"; exit 1; }
echo "OK: $n MIG instances"
