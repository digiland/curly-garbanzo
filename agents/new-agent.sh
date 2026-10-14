#!/usr/bin/env bash
# Create (or update) one user's cloud agent and print their pairing link.
#   ./new-agent.sh alice   (the Caimex key is shared: see set-api-key.sh)
set -euo pipefail
user="${1:?usage: new-agent.sh <user>}"
[[ "$user" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || { echo "user must be lowercase letters, digits, dashes" >&2; exit 1; }
base="https://notebooks.futurezim.com/agents/$user"
here="$(cd "$(dirname "$0")" && pwd)"

# Keep an existing password so paired phones stay signed in.
if ! kubectl -n agents get secret "agent-$user" >/dev/null 2>&1; then
  kubectl -n agents create secret generic "agent-$user" \
    --from-literal=password="$(openssl rand -base64 32 | tr -d '/+=' | cut -c1-40)" >/dev/null
fi

sed "s/USER_NAME/$user/g" "$here/agent.yaml.tmpl" | kubectl apply -f - >/dev/null
kubectl -n agents rollout status "deploy/agent-$user" --timeout=180s >/dev/null
"$here/pair-agent.sh" "$user"
