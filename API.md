# API Documentation

**KubeAstra AI Assistant** — Complete API reference for REST endpoints, MCP tools, and integrations.

---

## ⚡ Before You Start

**Prerequisites:**
- ✅ Both servers must be running (see setup docs below)
- ✅ Python 3.11+ and required dependencies installed
- ✅ kubectl configured or SSH access to cluster

**Setup & Start Servers:**

| Server | Setup Guide | Start Command | Port |
|--------|-------------|---------------|------|
| **REST Backend** | [`ui/backend/README.md`](ui/backend/README.md) | `cd ui/backend && uvicorn main:app --reload --port 8000` | 8000 |
| **MCP HTTP Server** | [`mcp/http_mcp/README.md`](mcp/http_mcp/README.md) | `cd mcp && make run-http` | 8001 |

**Quick Setup (one-liner for both):**
```bash
# Terminal 1 - REST Backend
cd ui/backend && uvicorn main:app --reload --port 8000

# Terminal 2 - MCP HTTP Server
cd mcp && make run-http
```

After starting, both servers should be accessible:
- ✓ REST API: http://localhost:8000/docs
- ✓ MCP Server: http://localhost:8001/tools/catalog

---

## Quick Links

| Purpose | URL | Format |
|---------|-----|--------|
| **Browse Tools** | `/tools/catalog` | Interactive HTML + JSON |
| **API Schema** | `/openapi.json` | OpenAPI 3.0 |
| **Swagger UI** | `/docs` | Interactive |
| **Tool Registry** | `/tools/categories` | JSON metadata |
| **Raw Tool List** | `/debug/tools` | JSON (internal) |
| **Health Check** | `/health` | JSON |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                  KubeAstra AI Assistant                    │
├─────────────────────┬───────────────────────────────────────┤
│   FastAPI Backend   │       MCP HTTP Server                 │
│   (port 8000)       │       (port 8001)                     │
├─────────────────────┼───────────────────────────────────────┤
│ REST Endpoints      │ Streamable HTTP + Debug Endpoints     │
│ • /api/chat         │ • /mcp/              (AI clients)     │
│ • /api/sessions     │ • /tools/catalog     (discovery)      │
│ • /api/health       │ • /docs              (Swagger)        │
│ • /api/*            │ • /openapi.json      (contract)       │
│                     │ • /debug/tools       (internal)       │
│                     │ • /debug/call        (internal)       │
└─────────────────────┴───────────────────────────────────────┘
            ↓                           ↓
       REST Clients              MCP Clients (Cursor, Claude)
       (Web UI, curl)            (IDE integration, agents)
```

---

## Three API Layers

### Layer 1: REST API (FastAPI Backend)

**Base URL:** `http://localhost:8000`

For **web UI, external clients, and manual HTTP calls**.

#### Endpoints

```bash
# Health & status
GET  /health              # Backend health check
GET  /api/health          # Same, with /api prefix

# Chat & sessions
GET  /api/sessions          # List chat sessions
POST /api/chat              # Send a chat message (single JSON response)
POST /api/chat/stream       # Send a chat message; receive SSE stream:
                            #   data: {"type": "iteration_planned", ...}
                            #   data: {"type": "step_complete",    ...}
                            #   data: {"type": "answer_start"}
                            #   data: {"type": "token", "text": "..."}
                            #   data: {"type": "answer_end"}
                            #   data: {"type": "done", "result": {...}}
POST /api/v1/agent/invoke   # Machine-to-machine structured agent invocation
POST /api/auth/signup       # Local signup when AUTH_ALLOW_SIGNUP=true
POST /api/auth/login        # Set HttpOnly local auth cookie
POST /api/auth/logout       # Clear auth cookie/session
GET  /api/auth/me           # Current local auth user and signup settings
GET  /api/sessions          # List account-owned chat sessions when auth is enabled
POST /api/sessions          # Create account-owned chat session
GET  /api/sessions/{id}/history     # Get session history
DELETE /api/sessions/{id}/history  # Wipe chat history AND per-user memory

# Feedback (Phase 1.3) — promote/quarantine captured chats
POST /api/feedback          # { capture_id, rating: "up"|"down", reason?, session_id?, prompt?, response?, tool_used? }
                            # rating=up → copy to verified runbook collection
                            # rating=down → delete from session_memory
                            # capture_id starting message: → audit-only SQLite feedback
                            # prompt/response are redacted snapshots for beta eval; raw Kubernetes output is not required
GET  /api/feedback/events   # query persisted feedback audit events
                            # filters: session_id, capture_id, rating=up|down,
                            # outcome=accepted|failed|rejected, limit

# Kubernetes operations
GET  /api/kubectl/{namespace}           # List resources
POST /api/kubectl/exec                  # Execute pod command
POST /api/kubectl/delete-pod            # Delete a pod
# ... and more

# Alerts & investigations (merged-in alert manager subsystem)
POST /api/v1/alerts/webhook   # Alertmanager webhook ingest (bearer-token auth)
POST /api/v1/alerts/manual    # /rca slash command — synthetic alert for any pod/workload
GET  /api/v1/alerts           # List recent investigations (paginated, ?limit=50)

# Auto-documented
GET  /docs                # Interactive Swagger UI
GET  /openapi.json        # OpenAPI 3.0 schema
```

#### Alerts endpoints — details

These are the merged-in alert manager API. Full operational runbook in
[docs/alert_manager_deploy.md](docs/alert_manager_deploy.md).

##### `POST /api/v1/alerts/webhook`

Accepts the standard Alertmanager webhook payload. Each alert in the batch
becomes its own investigation, dispatched to the orchestrator as a FastAPI
background task. Returns immediately with the investigation IDs.

**Auth:** bearer token via `Authorization: Bearer <token>` header. Backend
reads the expected token from `ALERT_WEBHOOK_TOKEN` env var (set by the
Helm chart from `secrets.alertWebhookToken_{Dev,Prod}` based on namespace).
When the env var is unset, the webhook stays open for local/dev. The
endpoint is exempt from the session-cookie middleware (`auth.is_public_path`).

**Request body:**
```json
{
  "status": "firing",
  "alerts": [
    {
      "status": "firing",
      "labels": {
        "alertname": "KubernetesPodCrashLooping",
        "namespace": "jenkins-legacy",
        "pod": "jenkins-legacy-0",
        "reason": "CrashLoopBackOff"
      },
      "annotations": {"summary": "Pod is crash looping"},
      "startsAt": "2026-06-26T15:00:00Z"
    }
  ]
}
```

**Response 200:**
```json
{"investigation_ids": ["<uuid>", "..."], "status": "accepted"}
```

**Response 401:** when `ALERT_WEBHOOK_TOKEN` is set and the bearer is missing or wrong.

##### `POST /api/v1/alerts/manual`

The `/rca` slash command's backing endpoint. Synthesizes an alert for a
pod or workload the user names in chat, auto-discovers the namespace via
`find_workload`, and aliases the synthetic alertname based on the pod's
observed status so a CrashLoopBackOff pod routes to the `crashloopbackoff`
specialty playbook (not generic-pod).

**Auth:** if `AUTH_ENABLED=true`, requires a valid user session; otherwise
treats caller as `local`.

**Request body:**
```json
{"target": "jenkins-legacy-0"}
```

Target formats:
- `<name>` — bare pod name; namespace auto-discovered, kind=pod
- `<kind>/<name>` — e.g. `pod/jenkins-legacy-0` or `deployment/api`
- `<namespace>/<kind>/<name>` — explicit fully-qualified target

**Response 200:**
```json
{"investigation_id": "<uuid>"}
```

##### `GET /api/v1/alerts`

Returns a paginated list of recent investigations, newest first. Each row
includes the full persisted document (alert, classification, evidence,
findings, RCA, audit log) for direct rendering by the UI.

**Query params:** `limit` (default 50).

**Response 200:**
```json
{
  "alerts": [
    {
      "id": "<uuid>",
      "namespace": "jenkins-legacy",
      "severity": "warning",
      "source": "alertmanager",
      "status": "completed",
      "created_at": "2026-06-26T15:00:14Z",
      "document": { "/* full Investigation model": "..." }
    }
  ]
}
```

#### Machine-to-machine agent invocation

`POST /api/v1/agent/invoke` is registered only when `AGENT_API_TOKEN` is
configured. It accepts arbitrary JSON input and returns a structured agent
result:

```bash
curl -X POST http://localhost:8000/api/v1/agent/invoke \
  -H "Authorization: Bearer $AGENT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "input": {"error": "ImagePullBackOff: manifest unknown"},
    "instruction": "Diagnose this error and provide remediation steps",
    "context": {"environment": "production"}
  }'
```

The endpoint is synchronous, read-oriented, rate-limited, and concurrency
bounded. `request_id` correlates the HTTP request; `run_id` identifies the
durable agent-harness trace. During rotation, both `AGENT_API_TOKEN` and
`AGENT_API_TOKEN_PREVIOUS` are accepted.

#### Usage Examples

```bash
# Test REST API
curl http://localhost:8000/health

# Open Swagger UI
open http://localhost:8000/docs

# Get API schema (for code generation)
curl http://localhost:8000/openapi.json | jq .
```

---

### Layer 2: MCP HTTP Transport (AI Clients)

**Base URL:** `http://localhost:8001`

For **AI clients: Cursor IDE, Claude API, custom agents**.

Uses the **MCP Streamable HTTP protocol** (March 2025+ spec).

#### Endpoints

```bash
# MCP Streamable HTTP
POST /mcp/                      # Main MCP transport (for AI clients)

# Auto-generated documentation
GET  /docs                      # Swagger UI for MCP server
GET  /openapi.json              # OpenAPI schema

# Debug/testing
GET  /debug/tools               # List all 48 tools with schemas
POST /debug/call                # Direct tool invocation
GET  /health                    # MCP server status
GET  /                          # Server info + quick start
```

#### MCP Tool Categories

Your 48 tools are organized into **10 categories**:

```
🔍 Discovery          (6 tools)   — Find workloads, pods, services, ConfigMaps
📋 Details            (10 tools)  — Deep resource inspection (pods, services, events, graph)
🔄 Context            (4 tools)   — Kubeconfig & cluster switching
📦 Repository         (3 tools)   — Search deployment configs
📊 Analysis           (5 tools)   — Pod/node/workload/namespace analysis
🧩 Plans              (3 tools)   — Multi-step remediation plans
📚 Knowledge Base     (1 tool)    — RAG semantic search
⎈ Helm                (5 tools)   — Read-only release inspection
🔧 Recovery           (5 tools)   — Remediation & scaling (WRITE ops)
🤖 AI Analysis        (6 tools)   — LLM-powered diagnostics
```

#### Usage Examples

```bash
# Test MCP health
curl http://localhost:8001/health

# List all available tools
curl http://localhost:8001/debug/tools | jq '.tools_count, [.tools[].name]'

# Browse tools in interactive UI
open http://localhost:8001/tools/catalog

# Call a tool directly (test/debug only)
curl -X POST http://localhost:8001/debug/call \
  -H "Content-Type: application/json" \
  -d '{"tool_name": "get_current_context", "arguments": {}}'

# Get tool categories
curl http://localhost:8001/tools/categories | jq '.categories | keys'
```

---

### Layer 3: Tool Discovery & Catalog

**Endpoints:** `/tools/catalog` and `/tools/categories`

For **DevOps team members exploring available tools**.

#### `/tools/catalog` — Interactive Browser

```bash
# HTML (interactive browser — open in web browser)
curl http://localhost:8001/tools/catalog

# JSON (machine-readable, for automation)
curl http://localhost:8001/tools/catalog?format=json
```

**Returns:**
- All 48 tools organized by category
- Tool names, descriptions, input schemas
- Write operation warnings
- Last updated timestamp

**Example JSON response:**

```json
{
  "metadata": {
    "total_tools": 48,
    "categories_count": 10,
    "last_updated": "2026-05-01T09:35:00Z",
    "mcp_protocol": "March 2025+"
  },
  "categories": {
    "kubectl_discovery": [
      {
        "name": "find_workload",
        "description": "Search for matching workloads (deployments, pods, services)...",
        "inputSchema": { ... }
      },
      ...
    ],
    ...
  }
}
```

#### `/tools/categories` — Metadata Only

```bash
# Get category overview without tool details
curl http://localhost:8001/tools/categories | jq .
```

**Returns:**
- Total tool count
- Category names and descriptions
- Tool count per category
- Simple tool name list

**Example response:**

```json
{
  "total_tools": 48,
  "categories": {
    "kubectl_discovery": {
      "description": "🔍 Discovery — Find workloads & resources",
      "tool_count": 6,
      "tools": ["find_workload", "get_pods", "get_namespaces", ...]
    },
    ...
  }
}
```

---

## Accessing the Endpoints

### ⚠️ First: Start Both Servers

Before accessing any endpoints, ensure both servers are running:

**Terminal 1 - Start REST Backend (port 8000):**
```bash
cd ui/backend
MCP_PATH=../../mcp PYTHONPATH=../../mcp uvicorn main:app --reload --port 8000
```

See: [`ui/backend/README.md`](ui/backend/README.md) for details

**Terminal 2 - Start MCP HTTP Server (port 8001):**
```bash
cd mcp
make run-http
```

See: [`mcp/http_mcp/README.md`](mcp/http_mcp/README.md) for details

**Verify both are running:**
```bash
# Check REST API
curl http://localhost:8000/health

# Check MCP Server
curl http://localhost:8001/health
```

---

### From Browser

Once both servers are running, open in your browser:

```
# Browse tools interactively
http://localhost:8001/tools/catalog

# Swagger UI for REST API
http://localhost:8000/docs

# Swagger UI for MCP HTTP server
http://localhost:8001/docs
```

### From Command Line

```bash
# List all tools
curl http://localhost:8001/tools/categories

# Download OpenAPI schema for REST API
curl http://localhost:8000/openapi.json > rest-api.json

# Download OpenAPI schema for MCP HTTP
curl http://localhost:8001/openapi.json > mcp-http.json

# Try a tool
curl -X POST http://localhost:8001/debug/call \
  -H "Content-Type: application/json" \
  -d '{"tool_name": "list_kubeconfig_contexts", "arguments": {}}'
```

### From Code

```python
# Python: Fetch tool catalog
import requests
response = requests.get('http://localhost:8001/tools/catalog?format=json')
tools = response.json()

for category, tools_list in tools['categories'].items():
    print(f"\n{category}: {len(tools_list)} tools")
    for tool in tools_list:
        print(f"  - {tool['name']}")
```

```javascript
// JavaScript: Fetch and display tools
const response = await fetch('http://localhost:8001/tools/catalog?format=json');
const catalog = await response.json();

Object.entries(catalog.categories).forEach(([category, tools]) => {
  console.log(`\n${category}: ${tools.length} tools`);
  tools.forEach(tool => console.log(`  - ${tool.name}`));
});
```

---

## API Contracts (OpenAPI)

### REST API Contract

```bash
# Get OpenAPI 3.0 schema for REST endpoints
curl http://localhost:8000/openapi.json
```

**Includes:**
- All `/api/*` endpoints
- Request/response schemas
- Authentication requirements
- Error codes

### MCP HTTP Server Contract

```bash
# Get OpenAPI 3.0 schema for MCP HTTP server
curl http://localhost:8001/openapi.json
```

**Includes:**
- MCP-specific endpoints
- Tool schemas (via `/debug/tools`)
- Health check
- Debug endpoints

---

## Authentication

### Optional Bearer Token (Team Deployments)

If running with `MCP_HTTP_AUTH_TOKEN` set, add `Authorization` header:

```bash
export TOKEN="my-secure-token"

# List tools with auth
curl http://localhost:8001/tools/catalog \
  -H "Authorization: Bearer $TOKEN"

# Call a tool with auth
curl -X POST http://localhost:8001/debug/call \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tool_name": "get_current_context", "arguments": {}}'
```

---

## Integration Guides

### Cursor IDE Setup

Edit `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "kubeastra-http": {
      "type": "http",
      "url": "http://127.0.0.1:8001/mcp/"
    }
  }
}
```

All 48 tools appear in Cursor's chat context immediately.

### Custom Agent Integration

```python
import asyncio
from mcp import ClientSession
from mcp.client.http import HTTPClientTransport

async def list_tools():
    transport = HTTPClientTransport("http://127.0.0.1:8001/mcp/")
    async with ClientSession(transport) as session:
        tools = await session.list_tools()
        for tool in tools.tools:
            print(f"- {tool.name}")

asyncio.run(list_tools())
```

### Code Generation

Generate TypeScript or Python clients from OpenAPI schema:

```bash
# Generate TypeScript client (using Swagger Codegen)
npm install -g swagger-codegen
swagger-codegen generate -i http://localhost:8000/openapi.json \
  -l typescript -o ./generated/rest-client

# Generate Python client (using OpenAPI Generator)
docker run --rm -v "${PWD}":/local openapitools/openapi-generator-cli generate \
  -i http://localhost:8001/openapi.json \
  -g python -o /local/generated/mcp-client
```

---

## Health Checks

### REST API Health

```bash
curl http://localhost:8000/health
```

**Response:**

```json
{
  "status": "ok",
  "backend": "ready",
  "kubectl": true,
  "gemini": true,
  "current_context": "docker-desktop"
}
```

### MCP Server Health

```bash
curl http://localhost:8001/health
```

**Response:**

```json
{
  "status": "ok",
  "transport": "streamable-http",
  "protocol": "MCP March 2025+",
  "mcp_server": "running",
  "tools_count": 48,
  "auth_enabled": false
}
```

---

## Error Handling

### REST API Errors

**400 Bad Request:**
```json
{
  "detail": [
    {
      "loc": ["body", "namespace"],
      "msg": "field required",
      "type": "value_error.missing"
    }
  ]
}
```

**404 Not Found:**
```json
{
  "detail": "Not found"
}
```

**500 Internal Server Error:**
```json
{
  "detail": "Internal server error"
}
```

### MCP HTTP Errors

**401 Unauthorized:**
```json
{
  "detail": "Missing or invalid Authorization header"
}
```

**503 Service Unavailable:**
```json
{
  "detail": "MCP server not initialized"
}
```

---

## Rate Limiting & Quotas

- **REST API:** No explicit rate limit (depends on backend resources)
- **MCP HTTP:** Same tool concurrency limits as stdio MCP
- **Kubectl operations:** Timeout enforced (default 15 seconds)
- **Log retrieval:** Max 200 lines per request (configurable)

---

## Environment Variables

### FastAPI Backend (port 8000)

```bash
MCP_PATH=../../mcp              # Path to MCP shared code
PYTHONPATH=../../mcp             # Python import path
GEMINI_API_KEY=...                          # Gemini API key
ALLOWED_NAMESPACES=prod,staging,dev         # Allowed k8s namespaces
KUBECTL_TIMEOUT_SECONDS=15                  # kubectl command timeout
MAX_LOG_TAIL_LINES=200                      # Max log lines returned
ENABLE_RECOVERY_OPERATIONS=false            # Allow WRITE operations

# Dry-run + single-use confirmation tokens for destructive ops
REQUIRE_DESTRUCTIVE_CONFIRMATION=true       # On by default; false restores legacy confirm=True only
CONFIRMATION_TOKEN_TTL_SECONDS=60           # Token expiry window

# Tool result summarization (off by default — opt-in for staging first)
ENABLE_LOG_SUMMARIZATION=false              # Master switch for logs/events/describe summarizer
LOG_SUMMARIZATION_THRESHOLD_BYTES=2048      # Only summarize outputs larger than this
LOG_SUMMARIZATION_USE_LLM=true              # false = heuristic-only (free, deterministic)
LOG_SUMMARIZATION_MAX_TOKENS=400            # Cap on the polish-call output

# RAG (Qdrant — Phase 1.1)
QDRANT_URL=http://localhost:6333            # Auto-set in Helm to in-cluster service
QDRANT_API_KEY=                             # Optional Bearer token
QDRANT_COLLECTION=k8s_errors                # Legacy collection name
QDRANT_TIMEOUT_SECONDS=10
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
EMBEDDING_DIM=384                           # Must match model

# Retrieval router (Phase 1.4) — auto-fires kb_search before every LLM call
RAG_ROUTER_ENABLED=true
RAG_ROUTER_TOP_K=5
RAG_ROUTER_CACHED_THRESHOLD=0.92            # Short-circuit threshold for verified runbooks
RAG_ROUTER_GROUNDED_THRESHOLD=0.70          # Threshold for injecting chunks as LLM context
RAG_ROUTER_COLLECTIONS=runbook,devops_doc   # Add session_memory once you trust unverified captures

# Session capture (Phase 1.3) — off until you've watched it on staging
SESSION_CAPTURE_ENABLED=false               # Master switch
SESSION_CAPTURE_TTL_DAYS=90                 # Unverified captures expire after this
SESSION_CAPTURE_TRANSCRIPT_CHARS=4000       # Soft cap on classifier prompt
SESSION_CAPTURE_REDACT_SECRETS=true         # Regex scrubber before persist

DB_PATH=./chat_history.db                   # SQLite database path (history, memory, cluster state, feedback audit)
```

### MCP HTTP Server (port 8001)

```bash
MCP_HTTP_HOST=127.0.0.1               # Bind address (0.0.0.0 for network)
MCP_HTTP_PORT=8001                    # Port
MCP_HTTP_PATH=/mcp                    # MCP transport path
MCP_HTTP_AUTH_TOKEN=...               # Bearer token (optional)
```

---

## Endpoints Summary Table

| Endpoint | Method | Server | Purpose | Auth |
|----------|--------|--------|---------|------|
| `/` | GET | 8001 | Server info | - |
| `/health` | GET | Both | Health check | - |
| `/docs` | GET | Both | Swagger UI | - |
| `/openapi.json` | GET | Both | OpenAPI schema | - |
| `/api/chat` | POST | 8000 | Single-response chat | Optional |
| `/api/chat/stream` | POST | 8000 | Streaming chat (Server-Sent Events) | Optional |
| `/api/feedback` | POST | 8000 | Promote/quarantine captured chats (Phase 1.3) | Optional |
| `/api/*` | GET/POST | 8000 | Other REST endpoints | Optional |
| `/mcp/` | POST | 8001 | MCP transport (48 tools) | Optional |
| `/tools/catalog` | GET | 8001 | Tool browser (categorized incl. Plans) | Optional |
| `/tools/categories` | GET | 8001 | Category metadata | Optional |
| `/debug/tools` | GET | 8001 | Raw tool list | Optional |
| `/debug/call` | POST | 8001 | Direct invocation | Optional |

---

## Troubleshooting

### Cannot connect to MCP server

```bash
# Check if MCP server is running
curl http://localhost:8001/health

# Check if tools are registered
curl http://localhost:8001/debug/tools | jq '.tools_count'

# Verify auth token (if required)
curl http://localhost:8001/health \
  -H "Authorization: Bearer $TOKEN"
```

### Tool not working

```bash
# List all available tools
curl http://localhost:8001/debug/tools | jq '.tools[].name'

# Try the tool with debug endpoint
curl -X POST http://localhost:8001/debug/call \
  -H "Content-Type: application/json" \
  -d '{"tool_name": "YOUR_TOOL_NAME", "arguments": {...}}'

# Check MCP server logs
tail -f k8s_devops_mcp.log
```

### Swagger UI not loading

```bash
# Verify FastAPI is running
curl http://localhost:8000/health
curl http://localhost:8001/health

# Check OpenAPI schema is valid
curl http://localhost:8000/openapi.json | jq .
curl http://localhost:8001/openapi.json | jq .
```

---

## Next Steps

1. **Start both servers** (see [Before You Start](#before-you-start) section):
   - REST Backend: `cd ui/backend && uvicorn main:app --reload --port 8000`
   - MCP HTTP: `cd mcp && make run-http`

2. **Verify servers are running:**
   - REST API: `curl http://localhost:8000/health`
   - MCP Server: `curl http://localhost:8001/health`

3. **Explore the tools:**
   - Interactive: Open [`http://localhost:8001/tools/catalog`](http://localhost:8001/tools/catalog)
   - Swagger: Visit [`http://localhost:8000/docs`](http://localhost:8000/docs)
   - JSON: `curl http://localhost:8001/tools/catalog?format=json`

4. **Configure AI clients:**
   - Cursor IDE: Edit `~/.cursor/mcp.json` (see [Cursor IDE Setup](#cursor-ide-setup))
   - Custom agents: Use the HTTP endpoint directly

5. **Read setup guides:**
   - REST Backend: [`ui/backend/README.md`](ui/backend/README.md)
   - MCP HTTP Server: [`mcp/http_mcp/README.md`](mcp/http_mcp/README.md)

---

**Last Updated:** 2026-05-01  
**Protocol:** MCP March 2025+ | OpenAPI 3.0 | REST (FastAPI)  
**Tools:** 48 registered (33 kubectl + 5 helm + 6 AI analysis + 3 multi-step plan + 1 knowledge base)
