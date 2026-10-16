#!/usr/bin/env bash
# Set (or rotate) the Caimex gateway key every agent uses, then restart the agents.
#   ./set-api-key.sh            (prompts for the key; it isn't echoed or kept in shell history)
set -euo pipefail
read -rsp "Caimex API key: " key; echo
[[ -n "$key" ]] || { echo "empty key" >&2; exit 1; }
printf '%s' "$key" | kubectl -n agents create secret generic caimex-shared \
  --from-file=api-key=/dev/stdin --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n agents rollout restart deploy -l component=agent
