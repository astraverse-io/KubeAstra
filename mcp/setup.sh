#!/bin/bash
# One-shot setup script for mcp (unified MCP server)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo "  K8s DevOps MCP Server — Setup"
echo "========================================"

# ── Python version check ──────────────────────────────────────────────────────
PYTHON=$(which python3.11 2>/dev/null || which python3 2>/dev/null || which python 2>/dev/null)
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.11+ required but not found"
    exit 1
fi

PY_VERSION=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "Using Python $PY_VERSION at $PYTHON"

MAJOR=$($PYTHON -c "import sys; print(sys.version_info.major)")
MINOR=$($PYTHON -c "import sys; print(sys.version_info.minor)")
if [ "$MAJOR" -lt 3 ] || ([ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 10 ]); then
    echo "ERROR: Python 3.10+ required (found $PY_VERSION)"
    exit 1
fi

# ── Virtual environment ───────────────────────────────────────────────────────
if [ ! -d "venv" ]; then
    echo ""
    echo "[1/4] Creating virtual environment..."
    $PYTHON -m venv venv
fi

echo "[2/4] Installing dependencies..."
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q
echo "  ✓ Dependencies installed"

# ── .env ──────────────────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    echo "[3/4] Creating .env from .env.example..."
    cp .env.example .env
    echo "  ✓ .env created — edit it and set GEMINI_API_KEY and ALLOWED_NAMESPACES"
else
    echo "[3/4] .env already exists, skipping"
fi

# ── Cursor mcp.json update ────────────────────────────────────────────────────
MCP_CONFIG="$HOME/.cursor/mcp.json"
echo "[4/4] Checking Cursor MCP configuration..."

if [ ! -f "$MCP_CONFIG" ]; then
    mkdir -p "$HOME/.cursor"
    cat > "$MCP_CONFIG" << EOF
{
  "mcpServers": {
    "k8s-devops": {
      "command": "$SCRIPT_DIR/venv/bin/python",
      "args": ["$SCRIPT_DIR/mcp_server/server.py"],
      "env": {
        "PYTHONPATH": "$SCRIPT_DIR",
        "ALLOWED_NAMESPACES": "prod,staging,dev,default"
      }
    }
  }
}
EOF
    echo "  ✓ Created $MCP_CONFIG"
else
    echo "  ✓ $MCP_CONFIG already exists — manually add the 'k8s-devops' entry if needed:"
    echo '    {'
    echo '      "command": "'"$SCRIPT_DIR"'/venv/bin/python",'
    echo '      "args": ["'"$SCRIPT_DIR"'/mcp_server/server.py"],'
    echo '      "env": {'
    echo '        "PYTHONPATH": "'"$SCRIPT_DIR"'",'
    echo '        "ALLOWED_NAMESPACES": "prod,staging,dev,default"'
    echo '      }'
    echo '    }'
fi

echo ""
echo "========================================"
echo "  Setup complete!"
echo "========================================"
echo ""
echo "Next steps:"
echo "  1. Edit .env and set:"
echo "     - GEMINI_API_KEY  (https://aistudio.google.com/)"
echo "     - ALLOWED_NAMESPACES  (comma-separated K8s namespaces)"
echo "     - DEPLOYMENT_REPO_URL  (optional, for repo search tools)"
echo ""
echo "  2. (Optional) Start Weaviate for RAG features:"
echo "     make docker-up"
echo "     make seed"
echo ""
echo "  3. Choose an MCP transport:"
echo "     - Local stdio: restart Cursor to load the MCP server"
echo "     - Local HTTP:  make run-http"
echo ""
echo "  4. In Cursor or another IDE, you now have the same K8s DevOps tools available"
echo "     - stdio config is written to ~/.cursor/mcp.json"
echo "     - HTTP endpoint defaults to http://127.0.0.1:8001/mcp/"
echo ""
echo "  Test with: 'investigate the pod my-app-xyz in namespace prod'"
echo ""
