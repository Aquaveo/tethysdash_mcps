# syntax=docker/dockerfile:1.7

# ---- Stage 1: builder -------------------------------------------------------
# Installs pinned Python dependencies into a venv. Build tooling stays here.
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Build deps for C-extension wheels not on PyPI (safety net; most wheels exist).
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
    && rm -rf /var/lib/apt/lists/*

COPY tethysdash_mcp/requirements.lock ./
RUN python -m venv /venv \
    && /venv/bin/pip install --upgrade pip \
    && /venv/bin/pip install -r requirements.lock


# ---- Stage 2: runtime -------------------------------------------------------
# Copies the venv from the builder. No build tooling. Non-root user.
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/venv/bin:$PATH \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=9000 \
    MCP_TRANSPORT=streamable-http

# wget for HEALTHCHECK; ca-certificates for HTTPS to the TethysDash backend
# (when TETHYSDASH_BASE_URL is set to an https:// endpoint).
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system mcp \
    && useradd --system --gid mcp --home /app --shell /usr/sbin/nologin mcp

WORKDIR /app
COPY --from=builder /venv /venv
COPY --chown=mcp:mcp tethysdash_mcp/ ./tethysdash_mcp/

USER mcp

# Read-only-filesystem safety (Cloud Run / hardened hosts): point HOME at the
# /tmp tmpfs so any library that touches $HOME/.cache or $HOME/.config has a
# writable target. The runtime plugin registry is read from tethysdash
# over HTTP (TETHYSDASH_BASE_URL/runtime-plugins/list/); no local
# filesystem state for plugins.
ENV HOME=/tmp

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD wget --quiet --spider --tries=1 \
        "http://127.0.0.1:${MCP_PORT}/health" || exit 1

ENTRYPOINT ["python", "-m", "tethysdash_mcp.mcp_server"]
