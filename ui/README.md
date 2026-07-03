# KubeAstra UI

A self-hosted web application that gives your team access to the 36 `mcp` tools through a conversational chat interface, without requiring Cursor or another AI IDE.

Users can ask natural language questions like *"are there any warnings in the cluster?"*, *"investigate pod my-app in the jenkins namespace"*, or *"show me the resource graph for demo namespace"*. The backend routes to the right Kubernetes tool, runs against the selected session cluster, and returns a concise LLM-powered summary.

---

## Architecture

```
Browser (port 3000)
    ↓  same-origin /api/* calls
Next.js frontend server (port 3000)
    ├── app/api/[...path]/route.ts  → runtime proxy to backend
    ├── app/chat/page.tsx           → primary chat UI
    ├── app/chat/[sessionId]        → shareable session route
    ├── components/ClusterConnect   → kubeconfig/context connection UI
    ├── components/ResourceGraph    → topology visualization
    └── lib/api.ts                  → typed API client using same-origin /api
    ↓  REST JSON
FastAPI backend (port 8000)
    ├── routers/chat.py      → chat router, optional ReAct, execute endpoint
    ├── routers/cluster.py   → kubeconfig autodetect/upload/context API
    ├── routers/sessions.py  → Chat history + SSH target API
    ├── routers/health.py    → /health and /api/health
    ├── react.py             → multi-step ReAct loop
    ├── db.py                → SQLite sessions/messages/SSH/cluster state
    └── request logging      → request_id + latency + tool dispatch logs
    ↓  Python imports (sys.path)
mcp/
    ├── tool_registry.py → shared tool metadata and dispatch helpers
    ├── ai_tools/       → LLM AI analysis
    ├── services/       → Gemini, self-hosted Ollama, Weaviate, embeddings
    └── k8s/         → kubectl live cluster access
         ├── kubectl_runner.py   → local cluster (kubeconfig)
         └── ssh_runner.py       → remote cluster (SSH + paramiko)
    ↓  kubectl (local) or SSH (remote)
Kubernetes cluster
```

The backend reuses `mcp` code directly — there is no duplication.

The frontend now proxies backend calls through its own `/api/*` route, so browser requests no longer depend on a baked-in backend URL.

---

## Key Features

- **Alerts dashboard (`/alerts`)** — receives Prometheus Alertmanager webhooks
  in real time and renders each as an investigation with full RCA, evidence,
  audit timeline, and recall of similar past incidents. Two-pane layout:
  sidebar lists recent investigations (severity / namespace / status chips
  + relative timestamps); detail pane renders the full `Investigation`
  document. **Auto-polls** every 5s while anything is `running`, stops when
  all rows are terminal — no perpetual background traffic. **↻ Refresh**
  button next to "Recent (N)" forces an immediate fetch with last-updated
  timestamp. **Namespace filter chips** above the list let you toggle
  individual namespaces in/out; the selection persists in localStorage so
  it survives reloads and polling cycles. Selection of the currently-open
  investigation is preserved across refreshes.
- **`/rca` slash command** — typing `/rca jenkins-legacy-0` in chat
  intercepts the message, posts to `/api/v1/alerts/manual`, and persists
  the result inline with the chat session. Auto-discovers the namespace
  and routes to a specialty playbook when the pod is in
  `CrashLoopBackOff` / `OOMKilled` — same RCA depth as a real Alertmanager
  webhook.
- **Chat interface** — natural language questions routed to the right kubectl tool automatically
- **Streaming chat responses** — `/api/chat/stream` (Server-Sent Events) emits real-time `iteration_planned` / `step_complete` events as the ReAct loop runs, plus token-by-token streaming of the final answer. UI pills update live with actual tool names (e.g. `investigate_pod` → `get_pod_logs`) instead of static placeholders.
- **Per-user conversation memory** — the agent remembers each session's recent namespaces, workloads, tools, and clusters (capped, 24h decay) so users don't have to re-specify "in prod" or "the same pod" on every turn. Stored alongside chat history in SQLite. Cleared automatically when "New chat" is clicked.
- **Retrieval-augmented answers (Phase 1)** — Qdrant knowledge base loaded from your team's markdown (nightly CronJob). Every chat turn is auto-routed: a verified runbook match returns cached without calling the LLM; a partial match grounds the LLM with citations. Live citations show up in the response payload.
- **Auto-capture + thumbs-up promotion** — resolved chats are auto-saved to a low-trust collection. 👍 / 👎 buttons next to each captured answer promote it to the verified runbook collection or quarantine it. Drives the agent's flywheel: every fix becomes a candidate runbook.
- **LLM-powered summaries** — results are synthesized into direct answers using Gemini or a self-hosted Ollama model
- **Session-scoped cluster connections** — autodetect local kubeconfig contexts or paste kubeconfig YAML, then select a context for the current session
- **Optional ReAct investigations** — set `USE_REACT_CHAT=true` for multi-step investigation with trace rendering
- **Shareable sessions** — copy a `/chat/<session-id>` link from the header
- **Resource graph visualization** — map namespace topology with health, edge labels, minimap, summary, and resource details
- **Approval-gated execute flow** — write commands require in-app approval and backend `ENABLE_RECOVERY_OPERATIONS=true`
- **Runtime backend proxying** — the frontend server proxies `/api/*` to the backend at runtime via `API_BASE_URL`, avoiding rebuilds just to change backend URLs
- **SSH remote cluster support** — enter host/username/password in the SSH panel to query any remote kubeadm cluster without copying kubeconfig files
- **SQLite session persistence** — chat history, SSH targets, and selected kubeconfig/context survive browser reloads
- **All-namespace queries** — "are there any warnings?" searches across all namespaces automatically
- **SSH reconnect banner** — if you reload the browser mid-session, a banner prompts for just the password to reconnect instantly
- **Request and tool logging** — backend logs now include request IDs, request latency, tool routing, tool dispatch timing, and SSH connection failures

---

## Quick Start (Local, No Docker)

### 1. Run setup

```bash
cd kubeastra-ai-assistant/ui
bash setup.sh
```

### 2. Edit backend `.env`

```bash
# backend/.env
LLM_PROVIDER=gemini                   # or ollama
GEMINI_API_KEY=your_key_here          # required when LLM_PROVIDER=gemini
# Or for a remote Ollama VM:
# OLLAMA_BASE_URL=http://10.0.0.25:11434
# OLLAMA_MODEL=qwen3:8b
USE_REACT_CHAT=false                  # set true for multi-step investigations
ALLOWED_NAMESPACES=demo,prod,staging,dev,default
ENABLE_RECOVERY_OPERATIONS=false      # set true for write ops
```

### 3. Start both services

```bash
bash start.sh          # starts backend (8000) + frontend (3000)
```

Open **http://localhost:3000/chat**

The frontend uses:

```bash
API_BASE_URL=http://localhost:8000
```

behind the scenes and proxies browser requests through `http://localhost:3000/api/*`.

For a standalone Ollama deployment on Rocky Linux, see `docs/internal_docs/OLLAMA_ROCKY_LINUX_VM_SETUP.md` (internal — not shipped with the repo).

---

## Quick Start (Docker Compose)

Requires Docker Desktop running.

```bash
cd kubeastra-ai-assistant/ui

# Copy and edit .env
cp backend/.env.example backend/.env
# edit backend/.env → set GEMINI_API_KEY or configure Ollama

# Start backend + frontend
docker compose up --build
```

Open **http://localhost:3000/chat**

The configured kubeconfig is mounted read-only into the backend container, giving it kubectl access to your local clusters. You can also connect per session from the UI by selecting **Connect Cluster** and pasting a kubeconfig.

The frontend container proxies requests to the backend container using:

```bash
API_BASE_URL=http://backend:8000
```

---

## Project Structure

```
ui/
├── backend/
│   ├── main.py              # FastAPI app — lifespan calls db.init_db()
│   ├── db.py                # SQLite layer (sessions, messages, ssh_targets, cluster_connections)
│   ├── react.py             # Optional ReAct multi-step investigation loop
│   ├── routers/
│   │   ├── chat.py          # POST /api/chat, /api/execute — router/ReAct + dispatcher
│   │   ├── cluster.py       # /api/cluster/* — autodetect/upload/connect/status/disconnect
│   │   ├── sessions.py      # GET/DELETE /api/sessions/{id}/history
│   │   │                    # GET/POST/DELETE /api/sessions/{id}/ssh-target
│   │   ├── ai_tools.py      # Legacy REST endpoints: /api/analyze, /fix, /runbook, ...
│   │   ├── kubectl.py       # Legacy REST endpoints: /api/pods, /events, /logs, ...
│   │   ├── recovery.py      # POST /api/exec, /delete-pod, /restart, /scale, /patch
│   │   └── health.py        # GET /health and /api/health
│   ├── requirements.txt     # Includes fastapi, uvicorn, paramiko, PyYAML
│   ├── .env.example
│   └── Dockerfile
├── frontend/
│   ├── app/
│   │   ├── api/[...path]    # Server-side proxy to backend runtime API base
│   │   ├── chat/page.tsx    # Main chat interface
│   │   ├── chat/[sessionId] # Shareable session route
│   │   ├── tools/page.tsx   # Legacy form-based tool dashboard
│   │   ├── layout.tsx
│   │   └── page.tsx         # Redirects to /chat
│   ├── lib/
│   │   └── api.ts           # Typed API client (same-origin /api proxy + legacy form helpers)
│   └── Dockerfile
├── docker-compose.yml
├── setup.sh                 # One-shot local setup
└── start.sh                 # Generated by setup.sh — starts both services
```

---

## Chat Interface (`/chat`)

The primary interface. Type any Kubernetes question and the AI routes it to the right tool automatically.

### Example queries

| What you type | What happens |
|---|---|
| `are there any warnings?` | `get_events --all-namespaces type=Warning` |
| `list pods in the jenkins namespace` | `get_pods -n jenkins` |
| `investigate pod my-app-xyz in prod` | Full kubectl playbook + LLM diagnosis |
| `what namespaces do I have?` | `kubectl get namespaces` |
| `list nodes` | `kubectl get nodes` |
| `get all resources in the platform namespace` | Aggregates pods, services, deployments, etc. |
| `show resource graph for demo namespace` | Renders namespace topology graph |
| `any recent events that need attention?` | `get_events --all-namespaces` |
| *(paste a raw error log)* | `analyze_error` → LLM root cause + fix commands |

### Cluster connection

Use **Connect Cluster** in the top bar to:

- autodetect contexts from the backend kubeconfig
- paste/upload kubeconfig YAML
- select a context for the current browser session
- disconnect and clean up temporary kubeconfig files

Chat and approved execute calls use the same session-selected cluster. This is separate from the SSH flow.

### SSH panel

Click the SSH icon in the top bar to connect to a remote kubeadm cluster. Enter:
- **Host** — IP or hostname of the master node
- **Username** — SSH user (e.g. `ubuntu`, `root`)
- **Password** — SSH password
- **Port** — defaults to `22`

All kubectl queries for that session are then routed over SSH to the remote cluster. Host, username, and port are saved to SQLite so a reconnect banner appears on page reload (password is never stored).

### ReAct mode

By default, chat uses the lower-latency single-shot router. Set:

```env
USE_REACT_CHAT=true
```

to enable multi-step ReAct investigations. ReAct can call multiple tools in one answer and stores trace metadata as `react_steps`, which the frontend renders in the assistant message.

### Session persistence

Each browser tab generates a unique session ID stored in `localStorage`. The backend saves every chat message to `chat_history.db` (SQLite), so history survives page reloads. Clicking **Share** copies `/chat/<session-id>`; clicking **New Chat** clears the current session's messages.

---

## Environment Variables

```bash
# backend/.env

# Path to mcp (only needed for local/non-Docker runs)
MCP_PATH=../../mcp

# LLM provider
LLM_PROVIDER=gemini                # gemini or ollama
GEMINI_API_KEY=...                 # required when LLM_PROVIDER=gemini
GEMINI_MODEL=gemini-3.1-flash-lite
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1
OLLAMA_AUTH_TOKEN=                 # optional if Ollama sits behind a reverse proxy
OLLAMA_TIMEOUT_SECONDS=120
USE_REACT_CHAT=false               # true enables multi-step ReAct

# kubectl tuning
ALLOWED_NAMESPACES=demo,prod,staging,dev,default
KUBECTL_TIMEOUT_SECONDS=15
MAX_LOG_TAIL_LINES=200
MAX_OUTPUT_BYTES=20000
ENABLE_RECOVERY_OPERATIONS=false   # set true to allow approved write ops

# SQLite persistence
DB_PATH=./chat_history.db          # path to SQLite file (default: next to main.py)

# RAG (Qdrant — Phase 1.1; replaced Weaviate)
QDRANT_URL=http://localhost:6333
QDRANT_API_KEY=
QDRANT_COLLECTION=k8s_errors
QDRANT_TIMEOUT_SECONDS=10
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
EMBEDDING_DIM=384
```

```bash
# frontend runtime env

# Server-side proxy target used by app/api/[...path]/route.ts.
# The browser still calls http://localhost:3000/api/*.
API_BASE_URL=http://localhost:8000
```

---

## API Reference

The FastAPI backend auto-generates interactive docs at:
- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc

### Key endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/chat` | Main chat endpoint — routes message and returns reply + tool result |
| `POST` | `/api/execute` | Execute approved kubectl command; writes require confirmation and recovery flag |
| `GET` | `/api/cluster/autodetect` | Find local kubeconfig contexts |
| `POST` | `/api/cluster/connect/kubeconfig` | Parse uploaded kubeconfig and return contexts |
| `POST` | `/api/cluster/connect/context` | Select and verify a session cluster context |
| `POST` | `/api/cluster/disconnect` | Disconnect session cluster and clean temp kubeconfig |
| `GET` | `/api/cluster/status/{session_id}` | Get current cluster connection status |
| `GET` | `/api/sessions/{id}/history` | Load chat history for a session |
| `DELETE` | `/api/sessions/{id}/history` | Clear chat history (New Chat) |
| `GET` | `/api/sessions/{id}/ssh-target` | Get saved SSH target for a session |
| `POST` | `/api/sessions/{id}/ssh-target` | Save SSH target (host/user/port — no password) |
| `DELETE` | `/api/sessions/{id}/ssh-target` | Remove saved SSH target |
| `GET` | `/health` | Health check (probe-friendly path) |
| `GET` | `/api/health` | Health check |

### Logging

The backend now emits:

- request-level logs with request ID, method, path, status, and elapsed time
- chat routing logs with selected tool and SSH usage
- tool dispatch timing logs
- SSH connection failure logs

---

## Deploying to a Team Server

Deploy on any Linux server with Docker and kubectl access:

```bash
# 1. Clone the repo on the server
git clone <your-repo> /opt/kubeastra-ai-assistant
cd /opt/kubeastra-ai-assistant/ui

# 2. For local cluster access — copy kubeconfig to server
scp ~/.kube/config server:/root/.kube/config

# 3. Create .env
cp backend/.env.example backend/.env
# edit backend/.env → set GEMINI_API_KEY

# 4. Build and start
docker compose up -d --build
```

For team access, point a DNS record at the server and put nginx or Traefik in front with HTTPS.

> **Tip:** If users access remote clusters via SSH, no kubeconfig needs to be on the central server at all — users provide SSH credentials through the chat UI per session.

> **Runtime config note:** The frontend no longer needs a rebuild just to point at a different backend URL. Set the frontend container's `API_BASE_URL` at runtime instead.
