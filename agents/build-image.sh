#!/usr/bin/env bash
# Build the agent image from a caimex-code checkout (branch futurezim-cloud) and load it into k3s.
#   ./build-image.sh [path-to-caimex-code]
set -euo pipefail
src="${1:-$HOME/caimex-code}"
here="$(cd "$(dirname "$0")" && pwd)"
(cd "$src" && PATH="$HOME/.bun-1.4.2/bin:$PATH" ./build-caimex.sh --single && git checkout -q packages/cli/package.json)
cp "$src/packages/cli/dist/cli-linux-x64/bin/caimex" "$here/image/caimex"
docker build -t local/caimex-agent:futurezim-cloud "$here/image"
rm -f "$here/image/caimex"
docker save local/caimex-agent:futurezim-cloud | k3s ctr images import -
