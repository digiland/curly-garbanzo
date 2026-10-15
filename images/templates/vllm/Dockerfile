# vLLM template: the notebook image plus an OpenAI-compatible LLM server.
#
# vLLM pins its own PyTorch (2.13 for 0.31), newer than the notebook's, so it
# lives in a separate venv and cannot break the notebook environment. The
# default PyPI wheels are built for CUDA 13, which this host's driver (575,
# CUDA <= 12.9) cannot run; the cu129 builds from vLLM's and PyTorch's own
# indexes are used instead. Check `nvidia-smi` before moving to a newer CUDA.
#
# torchcodec is the one dependency with no cu129 build for this torch: the
# PyPI wheel needs libnvrtc.so.13 and makes `vllm` itself fail to import. The
# CPU build replaces it; vLLM only uses it to decode video inputs.
ARG BASE=local/pytorch-caimex:cuda12-r3
FROM ${BASE}
ARG VLLM_VERSION=0.31.0
ARG CUDA_TAG=cu129
USER root
RUN python -m venv /opt/vllm \
 && /opt/vllm/bin/pip install --no-cache-dir uv \
 && VIRTUAL_ENV=/opt/vllm /opt/vllm/bin/uv pip install --no-cache \
      --extra-index-url "https://wheels.vllm.ai/${VLLM_VERSION}/${CUDA_TAG}" \
      --extra-index-url "https://download.pytorch.org/whl/${CUDA_TAG}" \
      --index-strategy unsafe-best-match \
      "vllm==${VLLM_VERSION}" \
 && VIRTUAL_ENV=/opt/vllm /opt/vllm/bin/uv pip install --no-cache --no-deps --reinstall \
      --index-url https://download.pytorch.org/whl/cpu "torchcodec==0.17.0+cpu" \
 && /opt/vllm/bin/python -c "import torch, torchcodec, vllm; print('vllm', vllm.__version__, 'torch', torch.__version__, 'cuda', torch.version.cuda)" \
 && chmod -R a+rX /opt/vllm
COPY zai-vllm /usr/local/bin/zai-vllm
RUN chmod 0755 /usr/local/bin/zai-vllm
USER ${NB_UID}
