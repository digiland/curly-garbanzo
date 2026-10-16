#!/usr/bin/env bash
# Print a fresh single-use pairing link (valid 5 minutes) for a user's agent.
#   ./pair-agent.sh alice
set -euo pipefail
user="${1:?usage: pair-agent.sh <user>}"
base="https://notebooks.futurezim.com/agents/$user"
password="$(kubectl -n agents get secret "agent-$user" -o jsonpath='{.data.password}' | base64 -d)"
code="$(printf 'Authorization: Bearer %s\n' "$password" |
  kubectl -n agents exec -i "deploy/agent-$user" -- curl -fsS -X POST -H @- http://127.0.0.1:4096/api/pair |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["code"])')"
echo "Agent:        $base"
echo "Pairing link: $base/auth/connect/$code   (single use, 5 minutes)"
command -v qrencode >/dev/null && qrencode -t ansiutf8 "$base/auth/connect/$code" || true
