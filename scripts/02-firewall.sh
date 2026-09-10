#!/usr/bin/env bash
# Close the box BEFORE k3s goes on - k3s binds its API to :6443 on all interfaces.
set -euo pipefail
command -v ufw >/dev/null || { echo "ufw not installed"; exit 1; }
ufw allow 22/tcp comment 'ssh'          # do this FIRST or you lock yourself out
ufw default deny incoming
ufw default allow outgoing              # the tunnel is outbound-only
ufw --force enable
ufw status verbose
echo
echo "NOTE: Docker bypasses ufw via the DOCKER-USER chain. Any container run with"
echo "      -p will be internet-exposed regardless of these rules."
