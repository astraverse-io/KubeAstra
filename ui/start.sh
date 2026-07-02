#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_DIR="$(cd "$SCRIPT_DIR/../mcp" && pwd)"

echo "Starting K8s DevOps Web UI..."

# Turbopack can exhaust macOS file descriptors on some local setups.
# Raise the per-shell limit when allowed; frontend dev uses webpack by default.
ulimit -n 65536 2>/dev/null || true

# Start backend
cd "$SCRIPT_DIR/backend"
mkdir -p .cache/huggingface
MCP_PATH="$MCP_DIR" PYTHONPATH="$MCP_DIR" HF_HOME="$SCRIPT_DIR/backend/.cache/huggingface" SENTENCE_TRANSFORMERS_HOME="$SCRIPT_DIR/backend/.cache/huggingface" HF_HUB_DISABLE_PROGRESS_BARS=1 HF_HUB_VERBOSITY=error TRANSFORMERS_VERBOSITY=error TOKENIZERS_PARALLELISM=false venv/bin/uvicorn main:app --port 8000 &
BACKEND_PID=$!
echo "Backend PID: $BACKEND_PID (port 8000)"

# Start frontend
cd "$SCRIPT_DIR/frontend"
API_BASE_URL=http://localhost:8000 npm run dev &
FRONTEND_PID=$!
echo "Frontend PID: $FRONTEND_PID (port 3000)"

echo ""
echo "Open: http://localhost:3000"
echo "Press Ctrl+C to stop both services"

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit" INT TERM
wait
