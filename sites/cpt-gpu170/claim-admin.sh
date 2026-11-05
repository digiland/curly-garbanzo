#!/usr/bin/env bash
# Create the `admin` account directly in the hub database, with a random
# password, so it can never be claimed by a stranger.
#
# NativeAuthenticator auto-approves a signup whose name is in admin_users. The
# generic flow closes that window by registering over a port-forward before the
# hostname goes live -- which works only if nobody forgets, and leaves `admin`
# claimable again if its row is ever lost. Writing the row here removes the race
# entirely: there is no moment at which `admin` exists without a password.
#
#   sudo sites/cpt-gpu170/claim-admin.sh [email]
#
# Two-factor is ON from the start: NativeAuthenticator only offers 2FA at
# signup, so an account created any other way could never get it. Prints the
# password and the authenticator secret ONCE. An admin that already exists is
# left untouched (exit 0), so deploy.sh can run this on every deploy.
set -euo pipefail
export KUBECONFIG="${KUBECONFIG:-/etc/kubernetes/admin.conf}"
EMAIL="${1:-}"

kubectl -n jupyterhub rollout status deploy/hub --timeout=120s >/dev/null
PASSWORD=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)

# Password goes over stdin, not argv, so it never appears in a process list.
OTP=$(printf '%s\n%s\n' "$PASSWORD" "$EMAIL" | kubectl -n jupyterhub exec -i deploy/hub -- python3 -c '
import sys, bcrypt
from jupyterhub.orm import new_session_factory
from nativeauthenticator.orm import UserInfo

password, email = sys.stdin.read().split("\n")[:2]
db = new_session_factory("sqlite:////srv/jupyterhub/jupyterhub.sqlite")()
if db.query(UserInfo).filter_by(username="admin").first():
    print("EXISTS"); sys.exit(0)
user = UserInfo(username="admin", email=email or None, is_authorized=True,
                has_2fa=True,
                password=bcrypt.hashpw(password.encode(), bcrypt.gensalt()))
db.add(user)
db.commit()
print(user.otp_secret)
')
if [ "$OTP" = EXISTS ]; then
    echo "admin already claimed - not changing it"
    exit 0
fi
echo
echo "  username: admin"
echo "  password: ${PASSWORD}"
echo "  2FA key:  ${OTP}    (add to an authenticator app as a time-based key)"
echo "  or scan:  otpauth://totp/ZAI:admin?secret=${OTP}&issuer=ZAI"
echo
echo "Shown once. Log in with the 6-digit code, then change the password at"
echo "/hub/change-password."
