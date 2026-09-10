#!/bin/bash
# Seed caimex config + shared credential into the user's home.
#
# $HOME is a per-user PVC mounted over the image, so anything baked under $HOME
# at build time is masked -- these files must be written at container start.
#
# start.sh SOURCES this file, so it must never call `exit` (that would kill the
# notebook startup) and must not leak shell options into the parent shell.
_caimex_seed() {
    local cfg="$HOME/.config/caimex-code"
    local auth="$HOME/.local/share/caimex-code"
    local cache="$HOME/.cache/caimex-code"

    mkdir -p "$cfg" "$auth" "$cache" 2>/dev/null || return 0
    [ -w "$cfg" ] || return 0

    # Default model. Written only if absent, so a user who switches models keeps it.
    if [ ! -f "$cfg/caimex.json" ]; then
        cat > "$cfg/caimex.json" <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
  "model": "${CAIMEX_MODEL:-caimex/Caimex2/deepseek-ai/DeepSeek-V4-Flash}"
}
EOF
    fi

    # Shared credential, refreshed each start so key rotation propagates without
    # rebuilding the image. The env var alone does NOT authenticate -- caimex
    # rejects CAIMEX_API_KEY with "Invalid authorization header"; the credential
    # must be in auth.json under the "caimex" provider.
    if [ -n "${CAIMEX_API_KEY:-}" ] && [ -w "$auth" ]; then
        printf '{"caimex":{"type":"api","key":"%s"}}\n' "$CAIMEX_API_KEY" > "$auth/auth.json"
        chmod 600 "$auth/auth.json" 2>/dev/null
    fi

    # Seed the model catalog; an unseeded first run stalls for minutes fetching it.
    local f
    for f in model-catalog.json models.json; do
        [ -f "$cache/$f" ] || cp "/opt/caimex/cache/$f" "$cache/$f" 2>/dev/null
    done
    return 0
}
_caimex_seed || true
unset -f _caimex_seed
