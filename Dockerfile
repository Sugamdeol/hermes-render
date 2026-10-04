# syntax=docker/dockerfile:1.7
# Native Hermes, without local ML models, Chromium or unrelated channel SDKs.
FROM node:24-bookworm-slim AS assets
ARG HERMES_REF=498bfc7bc12a937621b4215312049b1000726df3
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/hermes
RUN git init . && git remote add origin https://github.com/NousResearch/hermes-agent.git && git fetch --depth=1 origin ${HERMES_REF} && git checkout --detach FETCH_HEAD && rm -rf .git
RUN cd web && npm ci --no-audit --no-fund && npm run build
RUN cd ui-tui && npm install --no-audit --no-fund && npm run build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/hermes/.venv PATH=/opt/hermes/.venv/bin:$PATH \
    HERMES_HOME=/opt/data HERMES_TUI_DIR=/opt/hermes/ui-tui \
    HERMES_DASHBOARD=1 HERMES_DASHBOARD_HOST=127.0.0.1 HERMES_DASHBOARD_PORT=9119 \
    HERMES_DASHBOARD_TUI=1 \
    HERMES_AGENT_CACHE_MAX_SIZE=1 HERMES_AGENT_CACHE_IDLE_TTL_SECONDS=30 \
    HERMES_CRON_MAX_PARALLEL=1 HERMES_TUI_RPC_POOL_WORKERS=2 \
    MALLOC_ARENA_MAX=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates bash gosu tini nginx-light age ripgrep openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10000 hermes && useradd -m -u 10000 -g hermes -s /bin/bash hermes
COPY --from=assets /usr/local/bin/node /usr/local/bin/node
COPY --from=assets /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
COPY --from=assets --chown=hermes:hermes /opt/hermes /opt/hermes
WORKDIR /opt/hermes
RUN uv venv ${VIRTUAL_ENV} && uv pip install -e ".[web,mcp,pty,cli]" \
    "python-telegram-bot[webhooks]>=22.6,<23" "aiohttp>=3.13.3,<4" cryptography \
    && rm -rf /root/.cache/uv /opt/hermes/web/node_modules
COPY scripts/ /opt/render-tools/
RUN python /opt/render-tools/patch-lite.py /opt/hermes \
    && python /opt/render-tools/patch-model-discovery.py /opt/hermes/hermes_cli/model_switch.py \
    && chmod +x /opt/render-tools/*.sh /opt/render-tools/*.py /opt/hermes/docker/entrypoint.sh
COPY --chown=hermes:hermes skills/ /opt/render-tools/skills-local/
COPY --chown=hermes:hermes dashboard-plugins/ /opt/render-tools/dashboard-plugins/
COPY --chown=hermes:hermes env/ /opt/render-tools/env/
RUN mkdir -p /opt/data && chown hermes:hermes /opt/data \
    && touch /opt/hermes/ui-tui/packages/hermes-ink/dist/ink-bundle.js /opt/hermes/ui-tui/dist/entry.js
EXPOSE 10000
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/render-tools/bootstrap.sh"]
CMD ["sleep", "infinity"]
