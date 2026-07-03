# Backend

FastAPI backend for the KubeAstra UI.

This service exposes the REST API used by the Next.js frontend and imports logic directly from `mcp` so there is no duplicated Kubernetes or AI execution layer.

## What Changed

- Health checks now work on both `/health` and `/api/health`
- Added request logging middleware with request IDs and latency
- Added chat/tool dispatch logs in `routers/chat.py`
- Frontend integration now assumes same-origin `/api/*` calls from the browser, with the Next.js server proxying requests here
- **New streaming endpoint** `POST /api/chat/stream` returns `text/event-stream` with live ReAct step events (`iteration_planned`, `step_complete`) and token-by-token streaming of the final answer (`answer_start`, `token`, `answer_end`, `done`). Runs ReAct in a worker thread, bridges events to the SSE generator through an `asyncio.Queue`, includes `X-Accel-Buffering: no` so proxies don't batch the stream. The existing `POST /api/chat` is unchanged for programmatic clients.
- **Per-user conversation memory** (Phase 2.2) — `memory.py` records params from successful tool calls into a per-session JSON blob in SQLite; rendered as a short preamble injected into every LLM call so the agent doesn't keep asking the user to re-specify context. Failed tool calls are deliberately skipped so a wrong-namespace guess doesn't pollute memory. Cleared on `DELETE /api/sessions/<id>/history`.
- **RAG retrieval router** (Phase 1.4) — before every LLM call, the backend queries the Qdrant knowledge base via `services.rag.router.route()`. Three decisions: `cached` (short-circuits the LLM with a verified runbook), `grounded` (injects top chunks as LLM context), `cold` (current behavior). Both `/api/chat` and `/api/chat/stream` carry the decision in the response payload so the UI can render citations. Master switch: `RAG_ROUTER_ENABLED`.
- **Auto-capture from chats + feedback endpoint** (Phase 1.3) — after each chat that resolves a real problem, a classifier writes a redacted entry to Qdrant's `session_memory` (90-day TTL). When capture succeeds, the response payload carries a `capture_id`; thumbs-up/down promotes (up) that entry to the verified `runbook` collection or quarantines (down) it. If capture is skipped or Qdrant is unavailable, the frontend still sends `message:<session>:<message>` feedback, which is stored as audit-only SQLite feedback. Every feedback attempt is written to `feedback_events` with redacted prompt/assistant-answer snapshots for beta audit; query all events with `GET /api/feedback/events?limit=500`, thumbs-down events with `GET /api/feedback/events?rating=down`, or failures with `GET /api/feedback/events?outcome=failed`. Capture is off by default — flip `SESSION_CAPTURE_ENABLED=true` per environment.
- **Local account auth** — optional first-party username/password login (`AUTH_ENABLED=true`) stores users and auth sessions in SQLite with bcrypt password hashes and HttpOnly cookies. Authenticated users see their account-owned chat sessions across browsers. Create the first user without external SSO using `python -m scripts.create_user --username <name> --role admin`.

## Responsibilities

- serve chat requests at `POST /api/chat` (single-shot response) and `POST /api/chat/stream` (Server-Sent Events)
- persist chat history, per-user memory, SSH targets, and cluster selections in SQLite
- expose health, session, kubectl, AI, and recovery endpoints
- route natural-language chat requests into `mcp` wrapper/tool calls
- record per-session conversation memory from successful tool calls (Phase 2.2)
- switch to SSH-backed kubectl execution when per-request SSH credentials are supplied

## Runtime Flow

```text
Browser
  -> Next.js frontend on :3000
  -> frontend /api/* proxy
  -> FastAPI backend on :8000
  -> mcp shared logic
  -> kubectl / SSH / LLM provider / Weaviate
```

## Key Files

- [main.py](/Users/pruthvidavineni/AI_DevOps_Assistant/kubeastra-ai-assistant/ui/backend/main.py)
  App setup, middleware, request logging, lifespan init
- [db.py](/Users/pruthvidavineni/AI_DevOps_Assistant/kubeastra-ai-assistant/ui/backend/db.py)
  SQLite persistence (users, auth_sessions, sessions, messages, ssh_targets, cluster_connections, user_memory, feedback_events)
- [auth.py](/Users/pruthvidavineni/AI_DevOps_Assistant/kubeastra-ai-assistant/ui/backend/auth.py)
  Local auth helpers, cookie/session validation, and session ownership checks
- [memory.py](/Users/pruthvidavineni/AI_DevOps_Assistant/kubeastra-ai-assistant/ui/backend/memory.py)
  Per-user conversation memory (Phase 2.2): entity capture from tool params + preamble builder
- [react.py](/Users/pruthvidavineni/AI_DevOps_Assistant/kubeastra-ai-assistant/ui/backend/react.py)
  Multi-step ReAct loop with `on_event` callback (for streaming) and `memory_preamble` parameter (Phase 2.2). Runs a separate streaming LLM call for the final answer.
- [routers/chat.py](/Users/pruthvidavineni/AI_DevOps_Assistant/kubeastra-ai-assistant/ui/backend/routers/chat.py)
  Main chat router + SSE streaming endpoint + memory-capturing dispatch wrapper
- [routers/health.py](/Users/pruthvidavineni/AI_DevOps_Assistant/kubeastra-ai-assistant/ui/backend/routers/health.py)
  Health endpoints
- [routers/sessions.py](/Users/pruthvidavineni/AI_DevOps_Assistant/kubeastra-ai-assistant/ui/backend/routers/sessions.py)
  Chat history and SSH target endpoints (clear-history also wipes per-user memory)
- [routers/auth.py](/Users/pruthvidavineni/AI_DevOps_Assistant/kubeastra-ai-assistant/ui/backend/routers/auth.py)
  Local signup/login/logout/current-user endpoints
- [routers/feedback.py](/Users/pruthvidavineni/AI_DevOps_Assistant/kubeastra-ai-assistant/ui/backend/routers/feedback.py)
  Phase 1.3 — `POST /api/feedback` promotes/quarantines captured sessions and writes feedback audit events

## Local Run

```bash
cd ui/backend
MCP_PATH=../../mcp PYTHONPATH=../../mcp venv/bin/uvicorn main:app --reload --port 8000
```

## Health Endpoints

- `GET /health`
- `GET /api/health`

These return:

- backend status
- whether `kubectl` is available
- current kubectl context when available
- whether the configured LLM provider is enabled
- configured Weaviate URL

## Logging

The backend now logs:

- request ID
- HTTP method
- request path
- response status
- elapsed time in milliseconds
- selected chat tool
- tool dispatch duration
- SSH connection failures

This makes local debugging and container operations much easier.

## Environment Variables

```bash
MCP_PATH=../../mcp
PYTHONPATH=../../mcp
LLM_PROVIDER=gemini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-3.1-flash-lite
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1
OLLAMA_AUTH_TOKEN=
OLLAMA_TIMEOUT_SECONDS=120
ALLOWED_NAMESPACES=demo,prod,staging,dev,default
KUBECTL_TIMEOUT_SECONDS=15
MAX_LOG_TAIL_LINES=200
MAX_OUTPUT_BYTES=20000
ENABLE_RECOVERY_OPERATIONS=false

# Dry-run + confirmation tokens for destructive ops (on by default)
REQUIRE_DESTRUCTIVE_CONFIRMATION=true
CONFIRMATION_TOKEN_TTL_SECONDS=60

# Tool result summarization (off by default)
ENABLE_LOG_SUMMARIZATION=false
LOG_SUMMARIZATION_THRESHOLD_BYTES=2048
LOG_SUMMARIZATION_USE_LLM=true
LOG_SUMMARIZATION_MAX_TOKENS=400

DB_PATH=./chat_history.db

# RAG (Qdrant — Phase 1.1)
QDRANT_URL=http://localhost:6333
QDRANT_API_KEY=
QDRANT_COLLECTION=k8s_errors
QDRANT_TIMEOUT_SECONDS=10
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
EMBEDDING_DIM=384

# Retrieval router (Phase 1.4)
RAG_ROUTER_ENABLED=true
RAG_ROUTER_TOP_K=5
RAG_ROUTER_CACHED_THRESHOLD=0.92
RAG_ROUTER_GROUNDED_THRESHOLD=0.70
RAG_ROUTER_COLLECTIONS=runbook,devops_doc

# Session capture (Phase 1.3) — off by default
SESSION_CAPTURE_ENABLED=false
SESSION_CAPTURE_TTL_DAYS=90
SESSION_CAPTURE_TRANSCRIPT_CHARS=4000
SESSION_CAPTURE_REDACT_SECRETS=true
```

## Verification

```bash
python3 -m py_compile main.py routers/chat.py routers/health.py
```

Manual checks:

- `curl http://localhost:8000/health`
- `curl http://localhost:8000/api/health`
- open `http://localhost:3000/chat`
- submit a chat request and inspect backend logs for request IDs and tool timing
