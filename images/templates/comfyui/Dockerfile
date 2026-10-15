# ComfyUI template: the notebook image plus ComfyUI, opened in its own tab at
# /user/<name>/comfyui/ (jupyter-server-proxy starts it on first visit).
#
# ComfyUI gets a venv with --system-site-packages so it reuses the base
# image's CUDA PyTorch instead of downloading a second one, with torch pinned
# by a constraints file so its requirements cannot swap it out. Everything a
# user creates -- models, outputs, custom nodes -- goes to ~/ComfyUI on their
# own volume; the install in /opt stays read-only and identical for everyone.
ARG BASE=local/pytorch-caimex:cuda12-r3
FROM ${BASE}
ARG COMFYUI_REF=v0.40.0
USER root
RUN git clone --depth 1 --branch "${COMFYUI_REF}" https://github.com/comfyanonymous/ComfyUI /opt/comfyui \
 && python -m venv --system-site-packages /opt/comfyui/venv \
 && pip freeze | grep -iE '^(torch|torchvision|torchaudio|numpy)==' > /tmp/pins.txt \
 && /opt/comfyui/venv/bin/pip install --no-cache-dir -c /tmp/pins.txt -r /opt/comfyui/requirements.txt \
 && rm -f /tmp/pins.txt \
 && chmod -R a+rX /opt/comfyui
COPY zai-comfyui /usr/local/bin/zai-comfyui
COPY comfyui.svg /opt/zai/comfyui.svg
RUN chmod 0755 /usr/local/bin/zai-comfyui \
 && printf '%s\n' \
      'c.ServerProxy.servers.update({"comfyui": {' \
      '    "command": ["/usr/local/bin/zai-comfyui", "{port}"],' \
      '    "timeout": 180,' \
      '    "new_browser_tab": True,' \
      '    "launcher_entry": {"title": "ComfyUI", "icon_path": "/opt/zai/comfyui.svg"},' \
      '}})' \
      >> /etc/jupyter/jupyter_server_config.py
USER ${NB_UID}
