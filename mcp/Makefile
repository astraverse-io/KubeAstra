.PHONY: setup install env docker-up docker-down seed test lint clean help run run-http run-http-external

PYTHON := python3
VENV   := venv
PIP    := $(VENV)/bin/pip
PY     := $(VENV)/bin/python
SERVER := mcp_server/server.py
HTTP_SERVER := http_mcp/http_server.py

help:
	@echo "mcp — Unified K8s DevOps MCP Server"
	@echo ""
	@echo "  make setup       Full first-time setup (venv + install + .env)"
	@echo "  make install     Install Python dependencies"
	@echo "  make env         Copy .env.example → .env (if not exists)"
	@echo "  make docker-up   Start Weaviate vector DB (for RAG features)"
	@echo "  make docker-down Stop Weaviate"
	@echo "  make seed        Seed Weaviate with sample K8s/Ansible errors"
	@echo "  make run         Start the MCP server (for testing)"
	@echo "  make run-http    Start the MCP server over localhost HTTP (for IDE testing)"
	@echo "  make run-http-external Start the HTTP MCP server on 0.0.0.0"
	@echo "  make test        Run test suite"
	@echo "  make lint        Run mypy type checks"
	@echo "  make clean       Remove venv and caches"

setup: env install
	@echo ""
	@echo "✓ Setup complete. Next steps:"
	@echo "  1. Edit .env and set GEMINI_API_KEY and ALLOWED_NAMESPACES"
	@echo "  2. Run 'make docker-up' to start Weaviate (optional, for RAG)"
	@echo "  3. Run 'make seed' to populate the vector DB (optional)"
	@echo "  4. Add the server to ~/.cursor/mcp.json (see README.md)"

env:
	@if [ ! -f .env ]; then cp .env.example .env; echo "Created .env from .env.example"; fi

install:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

docker-up:
	docker compose up -d weaviate
	@echo "Weaviate started at http://localhost:8080"

docker-down:
	docker compose down

seed:
	$(PY) data/seed.py

run:
	PYTHONPATH=. $(PY) $(SERVER)

run-http:
	PYTHONPATH=. $(PY) $(HTTP_SERVER) --host 127.0.0.1 --port $${MCP_HTTP_PORT:-8001}

run-http-external:
	PYTHONPATH=. $(PY) $(HTTP_SERVER) --host 0.0.0.0 --port $${MCP_HTTP_PORT:-8001}

test:
	$(VENV)/bin/pytest tests/ -v --tb=short

lint:
	$(VENV)/bin/mypy mcp_server/ k8s/ services/ ai_tools/ config/ --ignore-missing-imports

clean:
	rm -rf $(VENV) __pycache__ .mypy_cache .pytest_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
