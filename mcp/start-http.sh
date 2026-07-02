#!/bin/bash
# Start the K8s DevOps MCP server over Streamable HTTP for Cursor.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$SCRIPT_DIR"

HOST="${MCP_HTTP_HOST:-127.0.0.1}"
PORT="${MCP_HTTP_PORT:-8001}"
MCP_PATH="${MCP_HTTP_PATH:-/mcp}"
PYTHON_BIN="${PYTHON_BIN:-$SCRIPT_DIR/venv/bin/python}"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "ERROR: Python virtualenv not found at $PYTHON_BIN"
    echo "Run ./setup.sh first, then retry ./start-http.sh"
    exit 1
fi

# Prefer an explicitly exported token. If not set, reuse the Bearer token from
# the workspace Cursor MCP config so Cursor and this server agree automatically.
if [ -z "${MCP_HTTP_AUTH_TOKEN:-}" ]; then
    WORKSPACE_MCP_CONFIG="$WORKSPACE_DIR/.cursor/mcp.json"
    USER_MCP_CONFIG="$HOME/.cursor/mcp.json"

    if [ -f "$WORKSPACE_MCP_CONFIG" ]; then
        MCP_HTTP_AUTH_TOKEN="$("$PYTHON_BIN" - "$WORKSPACE_MCP_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text())
header = (
    config.get("mcpServers", {})
    .get("k8s-devops", {})
    .get("headers", {})
    .get("Authorization", "")
)
print(header.split(" ", 1)[1] if header.startswith("Bearer ") else "")
PY
)"
    elif [ -f "$USER_MCP_CONFIG" ]; then
        MCP_HTTP_AUTH_TOKEN="$("$PYTHON_BIN" - "$USER_MCP_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text())
header = (
    config.get("mcpServers", {})
    .get("k8s-devops", {})
    .get("headers", {})
    .get("Authorization", "")
)
print(header.split(" ", 1)[1] if header.startswith("Bearer ") else "")
PY
)"
    fi
fi

echo "Starting K8s DevOps MCP HTTP server..."
echo "  Endpoint: http://$HOST:$PORT$MCP_PATH/"
if [ -n "${MCP_HTTP_AUTH_TOKEN:-}" ]; then
    echo "  Auth: enabled"
else
    echo "  Auth: disabled"
fi
echo ""
echo "Press Ctrl+C to stop"

export MCP_HTTP_AUTH_TOKEN
PYTHONPATH="$SCRIPT_DIR" "$PYTHON_BIN" http_mcp/http_server.py \
    --host "$HOST" \
    --port "$PORT" \
    --mcp-path "$MCP_PATH"
