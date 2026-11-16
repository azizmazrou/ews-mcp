# syntax=docker/dockerfile:1
# ews-mcp 4.5 — multi-stage, non-root from the start, import-gated build.

FROM python:3.11-slim AS builder
WORKDIR /src
COPY pyproject.toml ./
COPY ewsmcp ./ewsmcp
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir ".[extract]"

FROM python:3.11-slim
# Non-root from the first layer — no root+gosu dance. DATA_DIR points at a
# volume pre-chowned here so the unprivileged user can write it.
RUN groupadd -r mcp && useradd -r -g mcp -d /home/mcp -m mcp \
    && mkdir -p /data && chown mcp:mcp /data
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000
# Build gate: the application must IMPORT — never grep for version strings
# (the v3 Dockerfile's stale version-grep was a build landmine).
RUN python -c "import ewsmcp.main"
USER mcp
VOLUME /data
EXPOSE 8000
# /livez answers the moment the process is up (never-exit boot): a cold
# Exchange must NOT make the container unhealthy — that is /readyz's job.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=3).status==200 else 1)"
CMD ["ewsmcp"]
