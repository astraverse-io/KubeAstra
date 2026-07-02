#!/usr/bin/env bash
#
# local_dev.sh — one-command local dev environment for the K8s DevOps Assistant.
#
# Brings up the full stack on your laptop:
#   1. Qdrant (Docker)
#   2. MCP / backend (uvicorn, port 8000)
#   3. Frontend (next dev, port 3000)
#
# Run from the repo root:
#
#   ./scripts/local_dev.sh start       # bring everything up (default)
#   ./scripts/local_dev.sh stop        # stop backend + frontend, keep Qdrant
#   ./scripts/local_dev.sh stop --all  # also stop + remove Qdrant container
#   ./scripts/local_dev.sh status      # what's running, where the logs are
#   ./scripts/local_dev.sh logs [svc]  # tail logs (qdrant|backend|frontend|all)
#   ./scripts/local_dev.sh reindex     # re-index deployment repo into local Qdrant
#   ./scripts/local_dev.sh restart     # stop, then start
#
# State (logs, pids, generated configs) lives under ./.local-dev/ which is
# gitignored.
#
# Prereqs (the script checks these on first run):
#   - Docker Desktop running
#   - Python 3.10+ with the MCP venv at mcp/venv
#   - Python venv at ui/backend/venv
#   - Node.js 18+ and npm
#   - A .env at mcp/.env with GEMINI_API_KEY set

set -euo pipefail

# ── Paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

MCP_DIR="$REPO_ROOT/mcp"
BACKEND_DIR="$REPO_ROOT/ui/backend"
FRONTEND_DIR="$REPO_ROOT/ui/frontend"
DEPLOY_REPO_LOCAL="$REPO_ROOT/deployment-provisioning/ansible"

STATE_DIR="$REPO_ROOT/.local-dev"
LOG_DIR="$STATE_DIR/logs"
PID_DIR="$STATE_DIR/pids"
mkdir -p "$LOG_DIR" "$PID_DIR"

QDRANT_PORT=6333
BACKEND_PORT=8000
FRONTEND_PORT=3000

# ── Color output ──────────────────────────────────────────────────────────────

if [[ -t 1 ]]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'
  C_HEAD=$'\033[1;36m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
  C_OK= C_WARN= C_ERR= C_HEAD= C_DIM= C_RST=
fi

ok()   { printf "%s✓%s %s\n" "$C_OK"   "$C_RST" "$*"; }
warn() { printf "%s!%s %s\n" "$C_WARN" "$C_RST" "$*"; }
err()  { printf "%s✗%s %s\n" "$C_ERR"  "$C_RST" "$*" >&2; }
head() { printf "\n%s── %s%s\n" "$C_HEAD" "$*" "$C_RST"; }
dim()  { printf "%s%s%s\n"     "$C_DIM" "$*" "$C_RST"; }

# ── Pre-flight ────────────────────────────────────────────────────────────────

preflight() {
  head "Pre-flight checks"
  local fail=0

  command -v docker >/dev/null 2>&1 || { err "docker not on PATH"; fail=1; }
  docker info >/dev/null 2>&1 || { err "docker daemon not reachable (is Docker Desktop running?)"; fail=1; }
  command -v node   >/dev/null 2>&1 || { err "node not on PATH"; fail=1; }
  command -v npm    >/dev/null 2>&1 || { err "npm not on PATH"; fail=1; }

  if [[ ! -d "$MCP_DIR/venv" ]]; then
    err "MCP venv missing at $MCP_DIR/venv"
    dim "  → cd $MCP_DIR && python3 -m venv venv && venv/bin/pip install -r requirements.txt"
    fail=1
  fi
  if [[ ! -d "$BACKEND_DIR/venv" ]]; then
    err "Backend venv missing at $BACKEND_DIR/venv"
    dim "  → cd $BACKEND_DIR && python3 -m venv venv && venv/bin/pip install -r requirements.txt"
    fail=1
  fi

  if [[ ! -f "$MCP_DIR/.env" ]]; then
    warn "$MCP_DIR/.env not found — creating a template"
    cat > "$MCP_DIR/.env" <<'EOF'
# Paste your Gemini API key (https://aistudio.google.com/app/apikey):
GEMINI_API_KEY=

# Local Qdrant (no change needed):
QDRANT_URL=http://localhost:6333

# Phase 1.4 — RAG router (on by default):
RAG_ROUTER_ENABLED=true
RAG_ROUTER_COLLECTIONS=runbook,devops_doc,deployment_repo
RAG_ROUTER_GROUNDED_THRESHOLD=0.60

# Phase 1.3 — session capture (opt-in):
SESSION_CAPTURE_ENABLED=true
SESSION_CAPTURE_REDACT_SECRETS=true

# Phase 2.3 — semantic prompt cache (opt-in; needs session capture data to be useful):
PROMPT_CACHE_ENABLED=true
PROMPT_CACHE_THRESHOLD=0.95
PROMPT_CACHE_LOOKBACK_HOURS=24

# Phase 3.0 — proactive cluster triage (opt-in; needs kubeconfig pointed at a cluster):
ENABLE_PROACTIVE_TRIAGE=true

# ReAct multi-step chat — required for router + tool use:
USE_REACT_CHAT=true

# Embedding model (cached after first download):
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
EMBEDDING_DIM=384
EOF
    err "Set GEMINI_API_KEY in $MCP_DIR/.env, then re-run this script."
    fail=1
  else
    # File exists; sanity-check the key is filled in
    if ! grep -q "^GEMINI_API_KEY=.\+" "$MCP_DIR/.env"; then
      err "GEMINI_API_KEY is empty in $MCP_DIR/.env"
      fail=1
    fi
  fi

  if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
    warn "Frontend deps not installed — running npm install"
    (cd "$FRONTEND_DIR" && npm install >/dev/null 2>&1) || { err "npm install failed"; fail=1; }
    ok "npm install complete"
  fi

  [[ $fail -eq 0 ]] || exit 1
  ok "all checks passed"
}

# ── Process helpers ───────────────────────────────────────────────────────────

is_alive() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 1
  local pid
  pid=$(<"$pid_file")
  kill -0 "$pid" 2>/dev/null
}

stop_proc() {
  local name="$1"
  local pid_file="$PID_DIR/$name.pid"
  if is_alive "$pid_file"; then
    local pid
    pid=$(<"$pid_file")
    kill "$pid" 2>/dev/null || true
    # Give it 5s to clean up, then SIGKILL
    for _ in 1 2 3 4 5; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$pid_file"
    ok "stopped $name (pid $pid)"
  else
    dim "$name not running"
    rm -f "$pid_file"
  fi
}

port_in_use() {
  lsof -iTCP:"$1" -sTCP:LISTEN -P -n >/dev/null 2>&1
}

wait_for_port() {
  local port="$1"
  local name="$2"
  local seconds="${3:-30}"
  local i=0
  while ! port_in_use "$port"; do
    i=$((i + 1))
    if [[ $i -ge $seconds ]]; then
      err "$name did not start listening on :$port within ${seconds}s"
      dim "  tail the log:  tail -f $LOG_DIR/$name.log"
      return 1
    fi
    sleep 1
  done
}

# ── Start individual services ─────────────────────────────────────────────────

start_qdrant() {
  head "Starting Qdrant (Docker)"
  if curl -sS --max-time 2 "http://localhost:$QDRANT_PORT/readyz" >/dev/null 2>&1; then
    ok "Qdrant already up on :$QDRANT_PORT"
    return
  fi
  (cd "$MCP_DIR" && docker compose up -d qdrant) >"$LOG_DIR/qdrant.log" 2>&1
  # Wait for readiness (Qdrant takes a few seconds)
  local i=0
  while ! curl -sS --max-time 2 "http://localhost:$QDRANT_PORT/readyz" >/dev/null 2>&1; do
    i=$((i + 1))
    if [[ $i -ge 30 ]]; then
      err "Qdrant did not become ready in 30s. Check: docker logs \$(docker compose -f $MCP_DIR/docker-compose.yml ps -q qdrant)"
      return 1
    fi
    sleep 1
  done
  ok "Qdrant ready at http://localhost:$QDRANT_PORT"
}

start_backend() {
  head "Starting backend (uvicorn, port $BACKEND_PORT)"
  if is_alive "$PID_DIR/backend.pid"; then
    ok "backend already running (pid $(<"$PID_DIR/backend.pid"))"
    return
  fi
  if port_in_use "$BACKEND_PORT"; then
    err "Port $BACKEND_PORT is already in use by another process"
    dim "  Find it:  lsof -iTCP:$BACKEND_PORT -sTCP:LISTEN"
    return 1
  fi

  # Run from the backend dir so its venv + relative imports work
  (
    cd "$BACKEND_DIR"
    source venv/bin/activate
    # Load MCP .env so settings.py picks up GEMINI_API_KEY etc.
    export $(grep -v '^#' "$MCP_DIR/.env" | xargs)
    export MCP_PATH="$MCP_DIR"
    export PYTHONPATH="$MCP_DIR:${PYTHONPATH:-}"
    exec uvicorn main:app --host 0.0.0.0 --port "$BACKEND_PORT" --reload
  ) >"$LOG_DIR/backend.log" 2>&1 &
  echo $! > "$PID_DIR/backend.pid"
  wait_for_port "$BACKEND_PORT" "backend" 30 || return 1
  ok "backend up at http://localhost:$BACKEND_PORT (pid $(<"$PID_DIR/backend.pid"))"
}

start_frontend() {
  head "Starting frontend (next dev, port $FRONTEND_PORT)"
  if is_alive "$PID_DIR/frontend.pid"; then
    ok "frontend already running (pid $(<"$PID_DIR/frontend.pid"))"
    return
  fi
  if port_in_use "$FRONTEND_PORT"; then
    err "Port $FRONTEND_PORT is already in use by another process"
    dim "  Find it:  lsof -iTCP:$FRONTEND_PORT -sTCP:LISTEN"
    return 1
  fi
  (
    cd "$FRONTEND_DIR"
    # Point the Next dev server at the local backend
    export NEXT_PUBLIC_API_URL="http://localhost:$BACKEND_PORT"
    exec npm run dev -- --port "$FRONTEND_PORT"
  ) >"$LOG_DIR/frontend.log" 2>&1 &
  echo $! > "$PID_DIR/frontend.pid"
  wait_for_port "$FRONTEND_PORT" "frontend" 60 || return 1
  ok "frontend up at http://localhost:$FRONTEND_PORT (pid $(<"$PID_DIR/frontend.pid"))"
}

# ── Reindex ───────────────────────────────────────────────────────────────────

do_reindex() {
  head "Reindex deployment repo into local Qdrant"
  if [[ ! -d "$DEPLOY_REPO_LOCAL" ]]; then
    err "Local deployment-provisioning clone not found at $DEPLOY_REPO_LOCAL"
    dim "  → git clone https://github.com/kubeastra/deployment-provisioning.git $REPO_ROOT/deployment-provisioning"
    return 1
  fi
  if ! curl -sS --max-time 2 "http://localhost:$QDRANT_PORT/readyz" >/dev/null 2>&1; then
    err "Qdrant not running. Start it first:  $0 start"
    return 1
  fi
  (
    cd "$MCP_DIR"
    source venv/bin/activate
    export $(grep -v '^#' "$MCP_DIR/.env" | xargs)
    export RAG_CONFIG="$MCP_DIR/scripts/rag-config.local.yaml"
    python -m scripts.reindex
  )
  ok "reindex complete"
}

# ── Top-level commands ───────────────────────────────────────────────────────

cmd_start() {
  preflight
  start_qdrant
  start_backend
  start_frontend
  head "All services up 🚀"
  printf "  Frontend:  %shttp://localhost:%s%s\n" "$C_OK" "$FRONTEND_PORT" "$C_RST"
  printf "  Backend:   %shttp://localhost:%s%s\n" "$C_OK" "$BACKEND_PORT"  "$C_RST"
  printf "  Qdrant:    %shttp://localhost:%s%s\n" "$C_OK" "$QDRANT_PORT"   "$C_RST"
  printf "  Logs:      %s%s%s\n" "$C_DIM" "$LOG_DIR" "$C_RST"
  printf "\n  Next:      %s./scripts/local_dev.sh reindex%s     (one-time: index deployment repo)\n" \
    "$C_DIM" "$C_RST"
  printf "  Stop:      %s./scripts/local_dev.sh stop%s\n" "$C_DIM" "$C_RST"
}

cmd_stop() {
  head "Stopping services"
  stop_proc frontend
  stop_proc backend
  if [[ "${1:-}" == "--all" ]]; then
    head "Stopping Qdrant container"
    (cd "$MCP_DIR" && docker compose down) >>"$LOG_DIR/qdrant.log" 2>&1
    ok "qdrant stopped"
  else
    dim "qdrant left running (use './scripts/local_dev.sh stop --all' to stop it too)"
  fi
}

cmd_status() {
  head "Service status"
  if curl -sS --max-time 2 "http://localhost:$QDRANT_PORT/readyz" >/dev/null 2>&1; then
    ok "qdrant  — http://localhost:$QDRANT_PORT (ready)"
  else
    warn "qdrant  — not responding on :$QDRANT_PORT"
  fi
  if is_alive "$PID_DIR/backend.pid"; then
    ok "backend  — pid $(<"$PID_DIR/backend.pid"), http://localhost:$BACKEND_PORT"
  else
    warn "backend  — not running"
  fi
  if is_alive "$PID_DIR/frontend.pid"; then
    ok "frontend — pid $(<"$PID_DIR/frontend.pid"), http://localhost:$FRONTEND_PORT"
  else
    warn "frontend — not running"
  fi
  printf "\nLogs:  %s\n" "$LOG_DIR"
}

cmd_logs() {
  local svc="${1:-all}"
  case "$svc" in
    qdrant|backend|frontend)
      tail -f "$LOG_DIR/$svc.log"
      ;;
    all)
      head "Tailing all logs (Ctrl-C to stop)"
      tail -f "$LOG_DIR"/*.log
      ;;
    *)
      err "Unknown service '$svc' — use one of: qdrant, backend, frontend, all"
      exit 1
      ;;
  esac
}

cmd_restart() {
  cmd_stop
  sleep 1
  cmd_start
}

cmd_help() {
  cat <<EOF
local_dev.sh — local dev environment for the K8s DevOps Assistant

USAGE:
  ./scripts/local_dev.sh [command] [args]

COMMANDS:
  start           Bring everything up (default if no command given)
  stop            Stop backend + frontend (keeps Qdrant data volume)
  stop --all      Also stop + remove Qdrant container
  restart         stop then start
  status          What's running, where the logs are
  logs [svc]      Tail logs. svc = qdrant | backend | frontend | all (default)
  reindex         Re-index the deployment-provisioning repo into local Qdrant
  help            This message

URLs (after start):
  Frontend:  http://localhost:$FRONTEND_PORT
  Backend:   http://localhost:$BACKEND_PORT
  Qdrant:    http://localhost:$QDRANT_PORT

State lives under: $STATE_DIR
EOF
}

# ── Dispatch ─────────────────────────────────────────────────────────────────

case "${1:-start}" in
  start)   shift || true ; cmd_start   "$@" ;;
  stop)    shift || true ; cmd_stop    "$@" ;;
  restart) shift || true ; cmd_restart "$@" ;;
  status)  shift || true ; cmd_status  "$@" ;;
  logs)    shift || true ; cmd_logs    "$@" ;;
  reindex) shift || true ; do_reindex  "$@" ;;
  help|-h|--help) cmd_help ;;
  *)
    err "Unknown command: $1"
    cmd_help
    exit 1
    ;;
esac
