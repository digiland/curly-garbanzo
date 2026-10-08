# JupyterHub hub image + NativeAuthenticator (self-service signup/login).
# The tag MUST track the zero-to-jupyterhub chart version, or the hub and chart
# disagree about their own internals.
FROM quay.io/jupyterhub/k8s-hub:4.4.2
USER root
# qrcode renders the TOTP enrollment QR (SVG, no Pillow needed) served by the
# /hub/2fa page added in sites/cpt-gpu170/values.yaml.
RUN pip install --no-cache-dir jupyterhub-nativeauthenticator==1.2.0 qrcode==8.2
USER 1000
