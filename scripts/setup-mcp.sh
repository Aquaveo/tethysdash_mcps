#!/usr/bin/env bash
# setup-mcp.sh
#
# Creates a Python virtual environment, installs locked dependencies,
# and starts the TethysDash standalone MCP server.
#
# Usage:
#   ./scripts/setup-mcp.sh          # setup + run
#   ./scripts/setup-mcp.sh --setup  # setup only (no run)
#   ./scripts/setup-mcp.sh --run    # run only (skip setup)
#
# Environment variables honored at run time:
#   MCP_PORT                         default 9000
#   MCP_HOST                         default 127.0.0.1 (loopback only)
#   MCP_TRANSPORT                    default streamable-http
#   TETHYSDASH_BASE_URL              required for tools that proxy to tethysdash
#   TETHYSDASH_RUNTIME_REGISTRY_PATH default /tmp/runtimePluginRegistry.json
#   ALLOWED_ORIGINS                  default *
#
# See README.md for the full env-var reference.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv-mcp"
REQUIREMENTS="$PROJECT_DIR/tethysdash_mcp/requirements.lock"

setup() {
    echo "==> Setting up TethysDash MCP server environment"

    if [ ! -f "$REQUIREMENTS" ]; then
        echo "Error: requirements file not found at $REQUIREMENTS"
        echo "Run this script from a clean clone of the tethysdash_mcps repo."
        exit 1
    fi

    if [ ! -d "$VENV_DIR" ]; then
        echo "    Creating virtual environment at $VENV_DIR"
        python3 -m venv "$VENV_DIR"
    else
        echo "    Virtual environment already exists at $VENV_DIR"
    fi

    echo "    Installing locked dependencies..."
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet -r "$REQUIREMENTS"
    echo "    Done."
}

run() {
    if [ ! -d "$VENV_DIR" ]; then
        echo "Error: Virtual environment not found at $VENV_DIR."
        echo "Run setup first:"
        echo "  ./scripts/setup-mcp.sh --setup"
        exit 1
    fi

    local host="${MCP_HOST:-127.0.0.1}"
    local port="${MCP_PORT:-9000}"
    local transport="${MCP_TRANSPORT:-streamable-http}"
    local path="/mcp"
    [ "$transport" = "sse" ] && path="/sse"

    echo "==> Starting TethysDash MCP Server on http://${host}:${port}${path}"
    cd "$PROJECT_DIR"
    exec "$VENV_DIR/bin/python" -m tethysdash_mcp.mcp_server
}

case "${1:-}" in
    --setup)
        setup
        ;;
    --run)
        run
        ;;
    -h|--help)
        sed -n '1,20p' "$0"
        ;;
    *)
        setup
        run
        ;;
esac
