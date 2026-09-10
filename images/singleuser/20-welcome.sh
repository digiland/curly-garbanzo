#!/bin/bash
# Seed first-run orientation material into a brand-new home directory.
#
# Runs on every start but writes only once, tracked by a marker file: a user who
# deletes or edits the welcome notebook should not have it silently reappear.
#
# start.sh SOURCES this file: never call `exit`.
_welcome_seed() {
    local marker="$HOME/.jupyter-welcome-seeded"
    [ -f "$marker" ] && return 0
    [ -w "$HOME" ] || return 0

    cp /opt/welcome/Welcome.ipynb "$HOME/Welcome.ipynb" 2>/dev/null || true
    mkdir -p "$HOME/examples" 2>/dev/null
    cp /opt/welcome/examples/*.ipynb "$HOME/examples/" 2>/dev/null || true

    date -u +"%Y-%m-%dT%H:%M:%SZ" > "$marker" 2>/dev/null
    return 0
}
_welcome_seed || true
unset -f _welcome_seed
