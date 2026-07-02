#!/bin/bash
# One-command local setup for K8s DevOps Web UI (no Docker required)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_DIR="$(cd "$SCRIPT_DIR/../mcp" && pwd)"

echo "==========================================="
echo "  K8s DevOps Web UI — Local Setup"
echo "==========================================="

# ── Check dependencies ────────────────────────────────────────────────────────
command -v node >/dev/null 2>&1 || { echo "ERROR: Node.js required (https://nodejs.org)"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: Python 3.11+ required"; exit 1; }

# ── Backend venv ──────────────────────────────────────────────────────────────
echo ""
echo "[1/4] Setting up Python backend..."
cd "$SCRIPT_DIR/backend"

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q

# Reuse mcp venv packages if available, otherwise install MCP deps
if [ -f "$MCP_DIR/requirements.txt" ]; then
    venv/bin/pip install -r "$MCP_DIR/requirements.txt" -q 2>/dev/null || true
fi

# Create .env if missing
if [ ! -f ".env" ]; then
    if [ -f "$MCP_DIR/.env" ]; then
        ln -sf "$MCP_DIR/.env" .env
        echo "  Linked backend/.env to mcp/.env"
    else
        cp .env.example .env
        echo "  Created backend/.env — edit it to set GEMINI_API_KEY"
    fi
fi

mkdir -p .cache/huggingface

echo "  ✓ Backend ready"

# ── Frontend ──────────────────────────────────────────────────────────────────
echo ""
echo "[2/4] Installing frontend dependencies..."
cd "$SCRIPT_DIR/frontend"
npm install -q
echo "  ✓ Frontend ready"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "==========================================="
echo "  Setup complete!"
echo "==========================================="
echo ""
echo "Start the app (two terminals):"
echo ""
echo "  Terminal 1 — Backend:"
echo "    cd $SCRIPT_DIR/backend"
echo "    MCP_PATH=$MCP_DIR PYTHONPATH=$MCP_DIR HF_HOME=$SCRIPT_DIR/backend/.cache/huggingface SENTENCE_TRANSFORMERS_HOME=$SCRIPT_DIR/backend/.cache/huggingface HF_HUB_DISABLE_PROGRESS_BARS=1 HF_HUB_VERBOSITY=error TRANSFORMERS_VERBOSITY=error TOKENIZERS_PARALLELISM=false venv/bin/uvicorn main:app --reload --port 8000"
echo ""
echo "  Terminal 2 — Frontend:"
echo "    cd $SCRIPT_DIR/frontend"
echo "    API_BASE_URL=http://localhost:8000 npm run dev"
echo ""
echo "  Then open: http://localhost:3000"
echo ""
echo "  Or run both at once:"
echo "    bash $SCRIPT_DIR/start.sh"
echo ""

# Create start.sh helper
cat > "$SCRIPT_DIR/start.sh" << STARTEOF
#!/bin/bash
SCRIPT_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
MCP_DIR="\$(cd "\$SCRIPT_DIR/../mcp" && pwd)"

echo "Starting K8s DevOps Web UI..."

# Turbopack can exhaust macOS file descriptors on some local setups.
# Raise the per-shell limit when allowed; frontend dev uses webpack by default.
ulimit -n 65536 2>/dev/null || true

# Start backend
cd "\$SCRIPT_DIR/backend"
mkdir -p .cache/huggingface
MCP_PATH="\$MCP_DIR" \
PYTHONPATH="\$MCP_DIR" \
HF_HOME="\$SCRIPT_DIR/backend/.cache/huggingface" \
SENTENCE_TRANSFORMERS_HOME="\$SCRIPT_DIR/backend/.cache/huggingface" \
HF_HUB_DISABLE_PROGRESS_BARS=1 \
HF_HUB_VERBOSITY=error \
TRANSFORMERS_VERBOSITY=error \
TOKENIZERS_PARALLELISM=false \
venv/bin/uvicorn main:app --port 8000 &
BACKEND_PID=\$!
echo "Backend PID: \$BACKEND_PID (port 8000)"

# Start frontend
cd "\$SCRIPT_DIR/frontend"
API_BASE_URL=http://localhost:8000 npm run dev &
FRONTEND_PID=\$!
echo "Frontend PID: \$FRONTEND_PID (port 3000)"

echo ""
echo "Open: http://localhost:3000"
echo "Press Ctrl+C to stop both services"

trap "kill \$BACKEND_PID \$FRONTEND_PID 2>/dev/null; exit" INT TERM
wait
STARTEOF
chmod +x "$SCRIPT_DIR/start.sh"
echo "  Created start.sh for launching both services at once"
