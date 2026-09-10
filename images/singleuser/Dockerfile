FROM quay.io/jupyter/pytorch-notebook:cuda12-latest
USER root

# ---------------------------------------------------------------------------
# System tools. rclone is the object-storage client behind the `spaces` wrapper;
# git-lfs matters because model checkpoints in a repo are useless without it.
# ---------------------------------------------------------------------------
RUN apt-get update \
 && apt-get install -y --no-install-recommends rclone git-lfs less vim-tiny tree \
 && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Install caimex system-wide (outside $HOME, which is masked by the per-user PVC).
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# JupyterLab extensions. Each one replaces something people expect from Colab:
#   jupyter-resource-usage  -> the RAM/disk meter in the toolbar
#   jupyterlab-nvdashboard  -> GPU utilisation and VRAM, live
#   jupyterlab-git          -> "save a copy to GitHub"
#   nbgitpuller             -> "open notebook from GitHub" one-click links
# Sharing a running server with a colleague is handled by JupyterHub 5's own
# share feature (see charts/), NOT jupyter-collaboration: that package ships a
# @jupyter/ydoc pinned below what JupyterLab 4.6 requires, so its extension
# loads broken -- and because it *replaces* the core filebrowser and
# cell-executor extensions, installing it disables them and leaves users with a
# JupyterLab that has no file browser at all.
# boto3 is here so the object-storage examples work with no user setup.
# Versions are pinned: an unpinned lab extension is free to pull a build
# incompatible with the base image's JupyterLab on any future rebuild.
# ---------------------------------------------------------------------------
RUN pip install --no-cache-dir \
      jupyter-resource-usage==1.1.1 \
      jupyterlab-nvdashboard==0.13.0 \
      jupyterlab-git==0.51.0 \
      nbgitpuller==1.2.1 \
      boto3==1.40.29 \
 && jupyter lab build --minimize=False 2>/dev/null || true

# Show memory as a fraction of the pod's cgroup limit rather than the host's
# 235 GB, which is what the extension reports by default and is meaningless to
# someone holding a 12 GB grant.
RUN mkdir -p /etc/jupyter \
 && printf '%s\n' \
      'c.ResourceUseDisplay.track_cpu_percent = True' \
      'c.ResourceUseDisplay.mem_limit = 0  # 0 = read the cgroup limit' \
      >> /etc/jupyter/jupyter_server_config.py

ENV PATH=/opt/caimex/bin:$PATH

# ---------------------------------------------------------------------------
# First-run orientation material, copied into a fresh home by 20-welcome.sh.
# ---------------------------------------------------------------------------
COPY welcome /opt/welcome
RUN chmod -R a+rX /opt/welcome

COPY spaces /usr/local/bin/spaces
COPY 10-caimex.sh      /usr/local/bin/before-notebook.d/10-caimex.sh
COPY 20-welcome.sh     /usr/local/bin/before-notebook.d/20-welcome.sh
COPY 30-objectstore.sh /usr/local/bin/before-notebook.d/30-objectstore.sh
RUN chmod 0755 /usr/local/bin/spaces \
               /usr/local/bin/before-notebook.d/10-caimex.sh \
               /usr/local/bin/before-notebook.d/20-welcome.sh \
               /usr/local/bin/before-notebook.d/30-objectstore.sh

USER ${NB_UID}
