#!/bin/bash
# Platform glue run at every start. start.sh SOURCES this file: never `exit`.
#
# ~/.zai/README.md tells the user how to reach this server from outside: SSH,
# ports, the API. Rewritten every start so it always names the right user.
_zai_seed() {
    [ -w "$HOME" ] || return 0
    mkdir -p "$HOME/.zai" 2>/dev/null || return 0
    sed -e "s/{{USER}}/${JUPYTERHUB_USER:-jovyan}/g" -e "s/<host>/${ZAI_PUBLIC_HOST:-<host>}/g" /opt/zai/README.md > "$HOME/.zai/README.md" 2>/dev/null
    return 0
}
_zai_seed || true
unset -f _zai_seed
