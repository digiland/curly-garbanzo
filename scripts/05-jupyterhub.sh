#!/usr/bin/env bash
# Deploy JupyterHub. Requires CAIMEX_API_KEY (see .env.example).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
CHART_VERSION="${CHART_VERSION:-4.4.2}"

kubectl create namespace jupyterhub --dry-run=client -o yaml | kubectl apply -f -

echo "==> shared directories"
# Mounted into every server as ~/shared (read-only) and ~/team (read-write).
# 1000:100 is jovyan:users in the singleuser image; without this the mounts are
# root-owned and the team space is read-only in practice.
mkdir -p /srv/jupyter-shared/datasets /srv/jupyter-shared/team
chown -R 1000:100 /srv/jupyter-shared
chmod 755 /srv/jupyter-shared/datasets
chmod 2775 /srv/jupyter-shared/team   # setgid: files stay group-writable

echo "==> branding templates"
# A ConfigMap rather than a rebuild of the hub image, so copy and CSS changes
# are a helm upgrade instead of a docker build + ctr import.
kubectl -n jupyterhub create configmap hub-custom-templates \
  --from-file=templates/ --dry-run=client -o yaml | kubectl apply -f -

echo "==> platform layer (billing, sizes, templates, API) and the zai CLI"
# Checked before it reaches the hub: a catalog the module cannot load would
# leave the hub crash-looping and every user locked out.
python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" hub-ext/catalog.yaml \
  || { echo "hub-ext/catalog.yaml is not valid YAML"; exit 1; }
kubectl -n jupyterhub create configmap hub-zai \
  --from-file=hub-ext/zai_platform.py --from-file=hub-ext/zai_2fa.py \
  --from-file=hub-ext/zai_caimex.py --from-file=hub-ext/zai_access.py \
  --from-file=hub-ext/zai_credit.py --from-file=hub-ext/zai_myusage.py \
  --from-file=hub-ext/zai_payments.py \
  --from-file=hub-ext/catalog.yaml \
  ${ZAI_EXTRA_CATALOG:+--from-file=$ZAI_EXTRA_CATALOG} \
  --from-file=zai=cli/zai --dry-run=client -o yaml | kubectl apply -f -

echo "==> secrets"
if [ -n "${CAIMEX_API_KEY:-}" ]; then
    kubectl -n jupyterhub create secret generic caimex-credentials \
      --from-literal=api-key="$CAIMEX_API_KEY" --dry-run=client -o yaml | kubectl apply -f -
elif kubectl -n jupyterhub get secret caimex-credentials >/dev/null 2>&1; then
    # Re-running without a .env must not silently blank a working credential:
    # the previous version of this script overwrote the secret with "" and every
    # user's caimex would start failing to authenticate on the next spawn.
    echo "note: CAIMEX_API_KEY unset - keeping the existing caimex-credentials secret"
else
    echo "WARNING: CAIMEX_API_KEY unset - caimex will be installed but unauthenticated"
    kubectl -n jupyterhub create secret generic caimex-credentials \
      --from-literal=api-key="" --dry-run=client -o yaml | kubectl apply -f -
fi

# Optional. Absent keys leave the corresponding feature off: signups then wait
# for manual approval at /hub/authorize and the signup form has no captcha.
if [ -z "${RESEND_API_KEY:-}${RECAPTCHA_KEY:-}" ] && kubectl -n jupyterhub get secret hub-extras >/dev/null 2>&1; then
  echo "note: no hub-extras values in env - keeping the existing secret"
else
kubectl -n jupyterhub create secret generic hub-extras \
  --from-literal=resend-api-key="${RESEND_API_KEY:-}" \
  --from-literal=approval-from-email="${APPROVAL_FROM_EMAIL:-}" \
  --from-literal=self-approval-domains="${SELF_APPROVAL_DOMAINS:-}" \
  --from-literal=recaptcha-key="${RECAPTCHA_KEY:-}" \
  --from-literal=recaptcha-secret="${RECAPTCHA_SECRET:-}" \
  --dry-run=client -o yaml | kubectl apply -f -
fi

# Optional: Caimex accounts and Open Caimex (docs/CAIMEX.md). Like the other
# secrets, a run without the values keeps what is already there. The hub reads
# these only at start; the platform checksum below does not see them, so
# restart the hub after changing them.
if [ -n "${CAIMEX_SERVICE_PASSWORD:-}" ]; then
    kubectl -n jupyterhub create secret generic caimex-provision \
        --from-literal=api-url="${CAIMEX_API_URL:-}" \
        --from-literal=web-url="${CAIMEX_WEB_URL:-}" \
        --from-literal=org-id="${CAIMEX_ORG_ID:-}" \
        --from-literal=email="${CAIMEX_SERVICE_EMAIL:-}" \
        --from-literal=password="$CAIMEX_SERVICE_PASSWORD" --dry-run=client -o yaml | kubectl apply -f -
elif kubectl -n jupyterhub get secret caimex-provision >/dev/null 2>&1; then
    echo "note: CAIMEX_SERVICE_PASSWORD unset - keeping the existing caimex-provision secret"
else
    echo "note: CAIMEX_SERVICE_PASSWORD unset - users will not get Caimex accounts"
fi
# Optional online payment for credit (hub-ext/zai_payments.py, docs/PAYMENTS.md).
# The payer comes back to, and the provider's webhook calls, https://PUBLIC_HOSTNAME.
# The hub reads these only at start: restart it after changing them.
if [ -n "${ECO_PAYMENTS_CLIENT_SECRET:-}" ]; then
    kubectl -n jupyterhub create secret generic eco-payments \
        --from-literal=url="${ECO_PAYMENTS_URL:-}" \
        --from-literal=client-id="${ECO_PAYMENTS_CLIENT_ID:-}" \
        --from-literal=client-secret="$ECO_PAYMENTS_CLIENT_SECRET" \
        --from-literal=public-url="${PUBLIC_HOSTNAME:+https://$PUBLIC_HOSTNAME}" \
        --dry-run=client -o yaml | kubectl apply -f -
elif kubectl -n jupyterhub get secret eco-payments >/dev/null 2>&1; then
    echo "note: ECO_PAYMENTS_CLIENT_SECRET unset - keeping the existing eco-payments secret"
else
    echo "note: ECO_PAYMENTS_CLIENT_SECRET unset - online payment is off"
fi
# Optional S3-compatible object storage. Absent -> `spaces` reports it is not
# configured and notebooks start normally.
if [ -n "${S3_ENDPOINT:-}" ]; then
    kubectl -n jupyterhub create secret generic object-storage \
      --from-literal=endpoint="$S3_ENDPOINT" \
      --from-literal=region="${S3_REGION:-us-east-1}" \
      --from-literal=bucket="${S3_BUCKET:-}" \
      --from-literal=access-key="${S3_ACCESS_KEY:-}" \
      --from-literal=secret-key="${S3_SECRET_KEY:-}" \
      --dry-run=client -o yaml | kubectl apply -f -
else
    echo "note: S3_ENDPOINT unset - object storage disabled"
fi

helm repo add jupyterhub https://hub.jupyter.org/helm-chart/ >/dev/null 2>&1 || true
helm repo update jupyterhub
# The hub reads catalog.yaml and the module only at startup. Hashing them into
# a pod annotation makes helm roll the hub exactly when they change. Running
# servers are unaffected by a hub restart; the API is down for ~30 s.
ZAI_SUM=$(cat hub-ext/zai_platform.py hub-ext/zai_2fa.py hub-ext/zai_caimex.py hub-ext/zai_access.py hub-ext/zai_credit.py hub-ext/zai_myusage.py hub-ext/zai_payments.py hub-ext/catalog.yaml ${ZAI_EXTRA_CATALOG:-} cli/zai | sha256sum | cut -c1-16)
helm upgrade -i jupyterhub jupyterhub/jupyterhub \
  --namespace jupyterhub --version "$CHART_VERSION" \
  -f charts/jupyterhub-values.yaml ${EXTRA_VALUES:+-f $EXTRA_VALUES} \
  --set-string "hub.annotations.zai/platform-checksum=$ZAI_SUM" \
  --wait --timeout 10m

kubectl -n jupyterhub get pods
cat <<'MSG'

NEXT: claim the admin account BEFORE exposing this publicly.
NativeAuthenticator auto-approves a user named in admin_users, so whoever
registers as `admin` first becomes administrator -- including a stranger, if the
public URL is live and the account does not exist yet.

  kubectl -n jupyterhub port-forward svc/proxy-public 8080:http
  # from your laptop: ssh -L 8080:localhost:8080 root@<host>
  # browse http://localhost:8080/hub/signup and register as: admin
MSG
