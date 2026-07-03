# mcp — Unified Kubernetes DevOps MCP Server

A single, unified MCP (Model Context Protocol) server that merges the best of two projects:

| Source | What it brought |
|--------|----------------|
| `mcp-k8s-investigation-agent` | Live kubectl tools, multi-cluster support, recovery operations, deployment repo search |
| `k8s-ansible-mcp` | LLM-powered error analysis, RAG similarity search, fix playbooks, runbook generation |

**Result: 48 tools in one server**, covering the full DevOps loop — investigate → diagnose → fix → document.

The same toolset is available through two MCP transports:
- `stdio` for local IDE integration
- `streamable-http` for remote IDE or cross-workspace testing

---

## What's New vs. the Individual Projects

1. **`investigate_pod` now includes LLM analysis** — after running the kubectl playbook, it calls the configured provider to produce a root-cause analysis and copy-paste fix commands from the live data.
2. **Deterministic dependency evidence** — `investigate_pod` now scans all containers for dependency env vars (e.g. `KAFKA_ZOOKEEPER_CONNECT`, `KSQL_BOOTSTRAP_SERVERS`), verifies that the referenced services/endpoints exist in the namespace, and attaches a `evidence_summary` with `suspected_root_cause` and `suggested_fix` before AI analysis runs. This prevents the LLM from latching onto weak config smells while ignoring missing-service failures.
3. **Accurate all-namespace status filtering** — `get_pods(namespace="*", status_filter=...)` now uses per-namespace JSON parsing instead of the text-table parser, so container-level states like `CrashLoopBackOff` are correctly detected even when `kubectl` table output shows `Running`.
4. **Fuzzy pod name resolution** — partial names like `kafka` are resolved to real pod names like `my-kafka-0` by searching the target namespace. Works in both the UI chat dispatcher and the shared `tool_registry.py`.
5. **`start-http.sh` launcher** — one-command script to start the HTTP MCP server with auto-detected auth token from `.cursor/mcp.json`.
6. **SSH remote cluster support** — query any remote kubeadm cluster by passing SSH credentials (host/username/password). No kubeconfig copy needed on the central server.
7. **Session-scoped kubeconfig/context support** — `KubectlRunner` can be constructed with a selected kubeconfig path and context so web sessions target the right cluster.
8. **All-namespace queries** — `get_events` and `get_pods` accept `namespace="*"` to search across all namespaces.
9. **Tool registry metadata** — `tool_registry.py` centralizes tool names, aliases, surfaces, schemas, ReAct visibility, and write-operation safety metadata.
10. **Single Cursor config entry** — one `kubeastra` entry in `~/.cursor/mcp.json` replaces two.
11. **Unified settings** — one `.env` file covers kubectl tuning, Gemini, self-hosted Ollama, and RAG settings.

---

## Quick Start

```bash
cd kubeastra-ai-assistant/mcp
./setup.sh
```

Then edit `.env`:
```bash
LLM_PROVIDER=gemini
# or: LLM_PROVIDER=ollama
GEMINI_API_KEY=your_key_here
# or for a self-hosted / remote Ollama VM:
# OLLAMA_BASE_URL=http://10.0.0.25:11434
# OLLAMA_MODEL=qwen3:8b
ALLOWED_NAMESPACES=prod,staging,dev,default
```

Restart Cursor — the `kubeastra` MCP server will be active with all 48 tools.

If you want to replace Gemini with a standalone Ollama VM, the Rocky Linux runbook lives in `docs/internal_docs/OLLAMA_ROCKY_LINUX_VM_SETUP.md` (internal — not shipped with the repo; ask a maintainer if you need it).

To expose the server over localhost HTTP for another IDE:
```bash
./start-http.sh
# or: make run-http
```
Then point the IDE at:
```text
http://127.0.0.1:8001/mcp/
```

`start-http.sh` automatically reads the Bearer token from `.cursor/mcp.json` so Cursor and the server agree on auth without manual export.

---

## Project Structure

```
mcp/
├── mcp_server/
│   ├── server.py       # MCP entry point (stdio server)
│   ├── runtime.py      # Shared MCP bootstrap helpers
│   ├── tools.py        # All 48 tool registrations
│   └── schemas.py      # Pydantic input schemas for all tools
├── tool_registry.py    # Shared metadata for MCP/chat/ReAct/rest surfaces
├── tests/
│   └── test_tool_registry.py
├── http_mcp/
│   ├── http_server.py  # Streamable HTTP MCP endpoint at /mcp
│   ├── http_client.py  # Example HTTP MCP test client
│   └── README.md       # HTTP transport setup and usage
├── k8s/
│   ├── wrappers.py     # kubectl wrappers + AI-enhanced investigate_pod
│   ├── kubectl_runner.py  # Local/session kubectl runner + ContextVar routing
│   ├── ssh_runner.py   # SSH-based kubectl runner (paramiko) for remote clusters
│   ├── parsers.py
│   └── validators.py
├── services/
│   ├── llm/            # Gemini and self-hosted Ollama provider implementations
│   ├── llm_service.py  # LLM orchestration (analyze, summarize, investigate, runbook)
│   ├── vector_db.py    # Qdrant client (Phase 1.1 — replaced Weaviate)
│   ├── embeddings.py   # sentence-transformers
│   ├── error_parser.py # K8s + Ansible error classification (regex patterns)
│   ├── confirmation.py # Single-use confirmation tokens for destructive ops (Feature B)
│   ├── plans.py        # Multi-step remediation plans (Feature C / Phase 3.2)
│   ├── summarizer/     # Tool-output summarizers (logs, events, describe) (Phase 2.1)
│   └── rag/            # Phase 1 RAG pipeline:
│       ├── schema.py       #   collection specs (devops_doc, runbook, session_memory, k8s_errors)
│       ├── chunking.py     #   markdown-aware splitter
│       ├── ingestion.py    #   discover → chunk → embed → upsert
│       ├── sources/        #   local_path + git_repo source connectors
│       ├── router.py       #   retrieval router (cached/grounded/cold) — Phase 1.4
│       ├── capture.py      #   auto-capture worthy chats — Phase 1.3
│       ├── promotion.py    #   thumbs-up / quarantine handlers — Phase 1.3
│       └── redaction.py    #   regex secret scrubber before persisting
├── ai_tools/
│   ├── analyze.py      # analyze_error tool
│   ├── fix.py          # get_fix_commands + list_error_categories
│   ├── report.py       # cluster_report + error_summary
│   └── runbook.py      # generate_runbook
├── config/
│   └── settings.py     # Merged Pydantic settings (kubectl + AI + RAG)
├── data/
│   └── seed.py         # Seed Weaviate with sample K8s/Ansible errors
├── docker-compose.yml  # Qdrant only (for RAG features)
├── scripts/
│   └── reindex.py      # CronJob entrypoint for the doc ingestion pipeline
├── requirements.txt    # All dependencies
├── .env.example        # Template environment config
├── setup.sh            # One-shot setup
├── start-http.sh       # Start HTTP MCP server with auto-detected auth
└── Makefile            # Common tasks
```

---

## All 48 Tools

### Live kubectl Tools (33)

| Tool | What it does |
|------|-------------|
| `find_workload` | Search for pods/deployments/services by name across namespaces |
| `get_pods` | List pods in a namespace (or all namespaces with `namespace="*"`) with optional `status_filter` |
| `get_namespaces` | List all namespaces in the cluster |
| `get_nodes` | List cluster nodes, readiness, kubelet version, and OS image |
| `list_namespace_resources` | Aggregate view of all major resource types in a namespace (no ConfigMap data) |
| `list_services` | List all services in a namespace |
| `search_configmaps` | Find which ConfigMap in a namespace contains a value or key; returns CM, key, redacted excerpt |
| `get_configmap` | Read a named ConfigMap's data (redacted, size-capped); previews-only without a key |
| `describe_pod` | Full pod description with parsed highlights |
| `get_pod_logs` | Fetch current or previous container logs |
| `get_events` | Namespace events sorted by timestamp; use `namespace="*"` for all namespaces |
| `get_deployment` | Deployment status and replica counts |
| `get_service` | Service details and port config |
| `get_endpoints` | Check which pods back a service |
| `get_resource_graph` | Build a relationship graph across a namespace's resources |
| `get_rollout_status` | Monitor deployment rollout progress |
| `k8sgpt_analyze` | Run k8sgpt CLI analysis (optional) |
| `add_kubeconfig_context` | Add a cluster via SSH |
| `list_kubeconfig_contexts` | List available cluster contexts |
| `switch_kubeconfig_context` | Switch active cluster |
| `get_current_context` | Show active cluster |
| `search_deployment_repo` | Search Ansible/Helm repo for configs |
| `get_deployment_repo_file` | Read a file from the deployment repo |
| `list_deployment_repo_path` | Browse the deployment repo structure |
| `investigate_pod` ⭐ | Full triage: kubectl playbook + deterministic dependency checks + LLM diagnosis |
| `investigate_node` | Investigate a node's conditions, capacity, and allocated resources |
| `investigate_workload` | Investigate a deployment/statefulset/daemonset with pod health + AI analysis |
| `analyze_namespace` | Holistic health check of an entire namespace |
| `exec_pod_command` | Run a command inside a pod (requires confirm) |
| `delete_pod` | Force restart a pod (requires confirm + dry-run + single-use confirmation_token by default) |
| `rollout_restart` | Rolling restart a deployment (requires confirm + dry-run + single-use confirmation_token by default) |
| `scale_deployment` | Scale replicas up/down (requires confirm + dry-run + single-use confirmation_token by default) |
| `apply_patch` | Patch a K8s resource (requires confirm + dry-run + single-use confirmation_token by default) |

> **Destructive-op safety**: when `REQUIRE_DESTRUCTIVE_CONFIRMATION=true` (default), the four write tools above use a two-step ritual: call with `dry_run=True` to get a `kubectl --dry-run=server` preview and a fingerprint-bound 60s token, then call again with `confirm=True, confirmation_token=…` to execute. Tokens are single-use and bound to operation + target (and the patch body hash for `apply_patch`), so a token issued for one diff cannot execute a different one. Set the flag to `false` to restore the prior `confirm=True`-only behavior.

### Multi-Step Remediation Plans (3)

| Tool | What it does |
|------|-------------|
| `propose_remediation_plan` | Validate an ordered list of destructive steps (allow-listed to the four write tools), store as a 15-min TTL plan, return a `plan_id`. |
| `get_plan` | Retrieve a stored plan by id (status of each step). |
| `execute_plan_step` | Run one step from a stored plan. Caller must first call the underlying destructive tool with `dry_run=True` to obtain the `confirmation_token` for that step — approving the plan as a whole is not enough; humans approve every kubectl call. Uses atomic CAS to prevent two concurrent callers from clobbering each other's step status. |

### AI Analysis Tools (6)

| Tool | What it does |
|------|-------------|
| `analyze_error` | Paste any K8s/Ansible error → LLM root cause + fix commands |
| `get_fix_commands` | Get curated copy-paste fix commands for an error category |
| `list_error_categories` | List all 20+ supported error categories |
| `cluster_report` | Paste kubectl events → AI cluster health report |
| `error_summary` | Summarize a batch of errors from CI/CD logs |
| `generate_runbook` | Generate a full markdown runbook for a recurring error |

### Knowledge Base (1)

| Tool | What it does |
|------|-------------|
| `kb_search` | Semantic search over the ingested RAG knowledge base (team docs, runbooks, captured resolutions) with citations |

### Helm Tools (5)

Read-only Helm investigation. Runs on the same target as kubectl (local or SSH).

| Tool | What it does |
|------|-------------|
| `helm_available` | Detect whether Helm is installed/reachable on the active target; returns version |
| `list_helm_releases` | List releases in a namespace (or all namespaces when explicitly requested): name, revision, status, chart, app version |
| `get_helm_release` | Read a release's status/history/values (manifest/hooks/notes/metadata on request, revision=N for a past revision); redacted and capped |
| `diff_helm_revisions` | Unified diff of two revisions' values or manifest ("what changed in the last upgrade?"); redacted before diffing |
| `investigate_helm_release` | Composite: status + recent revisions + resources + live pod health and warnings, with a health assessment |

---

## Cluster Targeting

The MCP wrappers use a request-scoped runner (`ContextVar`) so callers can decide where `kubectl` runs:

- default local kubeconfig
- session-selected kubeconfig/context from the web UI
- SSH runner for remote kubeadm clusters

`KubectlRunner(kubeconfig_path=..., context=...)` is used by the web backend when a browser session selects a cluster context through `/api/cluster/*`.

---

## SSH Remote Cluster Support

The MCP server can query remote kubeadm clusters without a local kubeconfig by using SSH:

```python
# The kubectl_runner uses a ContextVar to switch between local and SSH runners.
# When SSH credentials are provided (via ui chat), all kubectl calls
# for that request are transparently routed over SSH to the remote master node.
```

This allows one central deployment to debug multiple `qa`/`dev`/`staging` clusters without
copying kubeconfig files onto the central server.

SSH verification is fail-closed. The target host key must already exist in
the path supplied as `known_hosts_path`, in `SSH_KNOWN_HOSTS_PATH`, or in the
process user's system known-hosts files. Unknown keys are rejected; the runner
never uses Paramiko `AutoAddPolicy`.

Machine remote diagnostics load authentication material from a read-only
credential directory containing exactly one of:

- `key`, with an optional `passphrase`; or
- `password`.

Credential files must be owner-only and remain inside their mounted directory.
The interactive `/api/chat` path retains direct password authentication for
compatibility, but it is subject to the same pinned-host-key requirement.
Optional bastion routing uses nested Paramiko clients and closes the inner
client, forwarding channel, and outer client together.

---

## Cursor Usage

Once set up, use natural language in Cursor chat:

```
investigate the pod payment-service-7d4f9b in namespace prod
```
→ Runs kubectl playbook + the configured LLM provider diagnosis in one shot.

```
I'm seeing this error in my CI/CD pipeline: [paste error]
```
→ Uses `analyze_error` for AI root cause + fix commands.

```
generate a runbook for pod_crashloop errors
```
→ Produces a Confluence-ready markdown runbook.

```
switch to the staging cluster and get events for the default namespace
```
→ Uses `switch_kubeconfig_context` then `get_events`.

---

## Configuration

All config lives in `.env`. Key variables:

```bash
# Required
ALLOWED_NAMESPACES=prod,staging,dev,default
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_key_here
# or when using a remote Ollama VM:
# OLLAMA_BASE_URL=http://10.0.0.25:11434
# OLLAMA_MODEL=qwen3:8b
# OLLAMA_AUTH_TOKEN=replace_me   # optional reverse-proxy bearer token
# OLLAMA_TIMEOUT_SECONDS=120

# Kubectl tuning
KUBECTL_TIMEOUT_SECONDS=15
MAX_LOG_TAIL_LINES=200
ENABLE_RECOVERY_OPERATIONS=false   # set true to enable write operations

# Dry-run + confirmation tokens for destructive ops (on by default)
REQUIRE_DESTRUCTIVE_CONFIRMATION=true
CONFIRMATION_TOKEN_TTL_SECONDS=60

# Tool result summarization (off by default — opt-in for staging first)
ENABLE_LOG_SUMMARIZATION=false
LOG_SUMMARIZATION_THRESHOLD_BYTES=2048
LOG_SUMMARIZATION_USE_LLM=true     # set false for heuristic-only (free, deterministic)
LOG_SUMMARIZATION_MAX_TOKENS=400

# RAG — Qdrant (Phase 1.1; replaced Weaviate). `make docker-up` starts it locally.
QDRANT_URL=http://localhost:6333
QDRANT_API_KEY=                                # set in production via Helm
QDRANT_COLLECTION=k8s_errors
QDRANT_TIMEOUT_SECONDS=10
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
EMBEDDING_DIM=384                              # must match the embedding model
```

---

## Cursor MCP Config (`~/.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "kubeastra": {
      "command": "<absolute-path>/kubeastra-ai-assistant/mcp/venv/bin/python",
      "args": ["<absolute-path>/kubeastra-ai-assistant/mcp/mcp_server/server.py"],
      "env": {
        "PYTHONPATH": "<absolute-path>/kubeastra-ai-assistant/mcp",
        "ALLOWED_NAMESPACES": "prod,staging,dev,default"
      }
    }
  }
}
```

Replace `<absolute-path>` with the output of `pwd` run from inside `kubeastra-ai-assistant/`.

## HTTP MCP Config (`~/.cursor/mcp.json` or another IDE)

Run the HTTP transport locally:

```bash
cd kubeastra-ai-assistant/mcp
make run-http
```

Then use this remote MCP config:

```json
{
  "mcpServers": {
    "kubeastra-http": {
      "url": "http://127.0.0.1:8001/mcp/"
    }
  }
}
```

Optional auth:

```bash
export MCP_HTTP_AUTH_TOKEN=dev-local-token
make run-http
```

```json
{
  "mcpServers": {
    "kubeastra-http": {
      "url": "http://127.0.0.1:8001/mcp/",
      "headers": {
        "Authorization": "Bearer dev-local-token"
      }
    }
  }
}
```

---

## Optional: Enable RAG (Qdrant)

The AI tools work without Qdrant — the configured LLM provider analyzes errors without past history.
With Qdrant enabled, `analyze_error` also returns similar past cases ranked by semantic similarity, and the agent gets a full retrieval pipeline (ingest team docs, search, route, capture). See [../docs/BEST_FEATURES_QUICKSTART.md](../docs/BEST_FEATURES_QUICKSTART.md) for end-to-end setup; the full operator reference (`QDRANT_DEPLOYMENT_GUIDE.md`) lives in `docs/internal_docs/`.

```bash
make docker-up   # starts Qdrant at http://localhost:6333
make seed        # loads ~60 sample K8s/Ansible errors into the k8s_errors collection

# Phase 1.2 — ingest your team's markdown docs into devops_doc:
cat > /tmp/rag.yaml <<'EOF'
sources:
  - kind: local_path
    path: /tmp/devops-docs
chunking: { max_tokens: 400, overlap_tokens: 60 }
EOF
RAG_CONFIG=/tmp/rag.yaml python -m scripts.reindex
```

---

## Makefile Commands

```bash
make setup            # First-time setup
make install          # Install/update dependencies
make docker-up        # Start Weaviate
make seed             # Seed vector DB
make run              # Start MCP server via stdio (for Cursor)
make run-http         # Start HTTP MCP server on 127.0.0.1:8001 (local IDE testing)
make run-http-external  # Start HTTP MCP server on 0.0.0.0:8001 (network-accessible)
make test             # Run tests
make clean            # Remove venv and caches
```
