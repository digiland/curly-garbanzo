FROM quay.io/jupyter/pytorch-notebook:cuda12-latest
USER root

# Install caimex system-wide (outside $HOME, which is masked by the per-user PVC).
RUN curl -fsSL https://caimex.econetai.co.zw/install.sh -o /tmp/install.sh \
 && CAIMEXCODE_INSTALL_DIR=/opt/caimex/bin bash /tmp/install.sh \
 && rm -f /tmp/install.sh \
 && ln -sf /opt/caimex/bin/caimex /usr/local/bin/caimex \
 && ln -sf /opt/caimex/bin/caimex /usr/local/bin/caimexcode \
 && chmod -R a+rX /opt/caimex

# Pre-warm the model catalog into an image-level cache so a user's first run is
# fast. Runs as root and leaves root-owned droppings under /home/jovyan, which
# would make the seed script unwritable -- clean them up in the same layer.
RUN mkdir -p /opt/caimex/cache /tmp/warm \
 && (HOME=/tmp/warm XDG_CONFIG_HOME=/tmp/warm/.config XDG_CACHE_HOME=/tmp/warm/.cache \
       timeout 180 /opt/caimex/bin/caimex models >/dev/null 2>&1 || true) \
 && cp /tmp/warm/.cache/caimex-code/*.json /opt/caimex/cache/ 2>/dev/null || true; \
    rm -rf /tmp/warm /home/jovyan/.config/caimex-code /home/jovyan/.cache/caimex-code \
           /home/jovyan/.local/share/caimex-code; \
    chown -R "${NB_UID}:${NB_GID}" /home/jovyan 2>/dev/null || true; \
    chmod -R a+rX /opt/caimex/cache; \
    echo "catalog cache:"; ls -la /opt/caimex/cache/

ENV PATH=/opt/caimex/bin:$PATH

COPY 10-caimex.sh /usr/local/bin/before-notebook.d/10-caimex.sh
RUN chmod 0755 /usr/local/bin/before-notebook.d/10-caimex.sh

USER ${NB_UID}
