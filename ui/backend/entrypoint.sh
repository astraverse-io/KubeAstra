#!/bin/bash
# ── Entrypoint — starts both the chat backend and the HTTP MCP server ─────────
#
# Port 8000 — FastAPI chat backend  (for the web UI)
# Port 8001 — HTTP MCP server       (for Cursor / Claude Desktop / any MCP client)
#
# Both share the same mcp tools, kubeconfig, and env vars.
# Stop either one and the container exits (so K8s restarts it cleanly).

set -e

echo "[entrypoint] Starting KubeAstra Assistant"
echo "[entrypoint]   Chat backend  → :8000"
echo "[entrypoint]   HTTP MCP      → :8001 (path: /mcp/)"
if [ "${MCP_HTTP_JSON_RESPONSE:-false}" = "true" ]; then
  echo "[entrypoint]   MCP response  → JSON"
  MCP_JSON_RESPONSE_ARG="--json-response"
else
  echo "[entrypoint]   MCP response  → SSE stream"
  MCP_JSON_RESPONSE_ARG=""
fi
if [ "${MCP_HTTP_STATELESS:-false}" = "true" ]; then
  echo "[entrypoint]   MCP sessions  → stateless"
  MCP_STATELESS_ARG="--stateless"
else
  echo "[entrypoint]   MCP sessions  → stateful"
  MCP_STATELESS_ARG=""
fi
if [ -n "$MCP_AUTH_TOKEN" ]; then
  echo "[entrypoint]   MCP auth      → bearer token enabled"
else
  echo "[entrypoint]   MCP auth      → DISABLED (set MCP_AUTH_TOKEN to enable)"
fi

# ── Chat backend (FastAPI) ────────────────────────────────────────────────────
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1 &
BACKEND_PID=$!

# ── HTTP MCP server ───────────────────────────────────────────────────────────
cd /app/mcp
python -m http_mcp.http_server --host 0.0.0.0 --port 8001 $MCP_JSON_RESPONSE_ARG $MCP_STATELESS_ARG &
MCP_PID=$!

echo "[entrypoint] Backend PID=$BACKEND_PID  MCP PID=$MCP_PID"

# ── Graceful shutdown on SIGTERM / SIGINT ─────────────────────────────────────
_term() {
  echo "[entrypoint] Shutting down..."
  kill "$BACKEND_PID" "$MCP_PID" 2>/dev/null
  wait "$BACKEND_PID" "$MCP_PID" 2>/dev/null
  exit 0
}
trap _term SIGTERM SIGINT

# Wait — exit if either process dies so K8s restarts the pod
wait -n "$BACKEND_PID" "$MCP_PID"
EXIT_CODE=$?
echo "[entrypoint] A process exited with code $EXIT_CODE — stopping container"
kill "$BACKEND_PID" "$MCP_PID" 2>/dev/null
exit $EXIT_CODE
