#!/bin/bash
# Configure S3-compatible object storage (DigitalOcean Spaces) for this user.
#
# Home is a per-user PVC that masks anything baked into the image, so the rclone
# config has to be written at container start, not at build time.
#
# start.sh SOURCES this file: never call `exit`, and leave no shell options behind.
_objectstore_seed() {
    [ -n "${S3_ENDPOINT:-}" ] || return 0
    [ -n "${AWS_ACCESS_KEY_ID:-}" ] || return 0
    [ -n "${S3_BUCKET:-}" ] || return 0

    local user="${JUPYTERHUB_USER:-${NB_USER:-jovyan}}"
    local cfgdir="$HOME/.config/rclone"
    mkdir -p "$cfgdir" 2>/dev/null || return 0
    [ -w "$cfgdir" ] || return 0

    # Rewritten every start so key rotation propagates without a rebuild.
    # 0600: the PVC is per-user, but the secret should not be world-readable
    # inside the container either.
    cat > "$cfgdir/rclone.conf" <<EOF
# Generated at container start by 30-objectstore.sh -- edits will be overwritten.
[spaces]
type = s3
provider = DigitalOcean
env_auth = true
endpoint = ${S3_ENDPOINT}
region = ${S3_REGION:-us-east-1}
acl = private

[myspace]
type = alias
remote = spaces:${S3_BUCKET}/users/${user}
EOF
    chmod 600 "$cfgdir/rclone.conf" 2>/dev/null

    # aws-cli and boto3 both read this; AWS_* creds already come from the env.
    mkdir -p "$HOME/.aws" 2>/dev/null
    if [ ! -f "$HOME/.aws/config" ]; then
        cat > "$HOME/.aws/config" <<EOF
[default]
region = ${S3_REGION:-us-east-1}
endpoint_url = ${S3_ENDPOINT}
EOF
    fi

    # Ensure the user's own prefix exists so `spaces ls` isn't an error on day one.
    timeout 20 rclone mkdir "myspace:" >/dev/null 2>&1 || true

    # Optional FUSE mount, mirroring Colab's drive.mount(). Unprivileged
    # containers have no /dev/fuse, so this is normally skipped -- the CLI and
    # boto3 paths above are the supported interface. Backgrounded and capped so
    # a hung mount can never delay notebook startup.
    if [ -e /dev/fuse ] && [ -w /dev/fuse ]; then
        mkdir -p "$HOME/spaces" 2>/dev/null
        ( timeout 30 rclone mount "myspace:" "$HOME/spaces" \
            --daemon --vfs-cache-mode writes >/dev/null 2>&1 || true ) &
    fi
    return 0
}
_objectstore_seed || true
unset -f _objectstore_seed
