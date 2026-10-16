# One caimex agent server per user. Built from digiland/caimex-code branch futurezim-cloud.
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git curl ripgrep python3 less procps tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /bin/bash agent
COPY caimex /usr/local/bin/caimex
USER agent
WORKDIR /home/agent
ENV HOME=/home/agent TMPDIR=/tmp
EXPOSE 4096
# OPENCODE_PASSWORD comes from the pod's secret; caimex removes it from the tools' environment.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "mkdir -p /home/agent/work && cd /home/agent/work && exec caimex serve --hostname 0.0.0.0 --port 4096"]
