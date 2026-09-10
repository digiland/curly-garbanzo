# JupyterHub hub image + NativeAuthenticator (self-service signup/login).
# The tag MUST track the zero-to-jupyterhub chart version, or the hub and chart
# disagree about their own internals.
FROM quay.io/jupyterhub/k8s-hub:4.4.2
USER root
RUN pip install --no-cache-dir jupyterhub-nativeauthenticator==1.2.0
USER 1000
