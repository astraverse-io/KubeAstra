# Gemini Dependency Removal — Analysis & Options

This document captures the analysis of removing the Google Gemini API dependency from the `ui` + `mcp` stack. It covers where Gemini is used, how each tool is affected, the tradeoffs involved, and alternatives to consider.

---

## Where Gemini Is Used

Gemini plays two distinct roles in the system.

### Role 1 — Chat Intent Router (`ui/backend/routers/chat.py`)

When a user types a message in the chat window, `POST /api/chat` calls Gemini to:

- Classify the intent (e.g. "my pod is crashing" → `analyze_error`)
- Extract parameters from natural language (e.g. namespace, pod name, deployment name)
- Return a structured JSON routing decision

**This role already has a keyword-based fallback** (`_keyword_route`) built into `chat.py`. If `GEMINI_API_KEY` is missing, the fallback activates automatically. It handles common patterns using regex but is less accurate for ambiguous or multi-part messages.

### Role 2 — AI Analysis Engine (`mcp/services/llm_service.py`)

This is where the core AI capability lives. Gemini powers four distinct operations:

| Method | Called By | What It Does |
|---|---|---|
| `analyze()` | `ai_tools/analyze.py`, `ai_tools/fix.py` | Diagnoses error, produces root cause, solution steps, fix commands |
| `analyze_live_investigation()` | `k8s/wrappers.py` (`investigate_pod`) | Analyzes live kubectl data (describe, logs, events) for a specific pod |
| `generate_runbook()` | `ai_tools/runbook.py` | Generates a full markdown runbook for an error category |
| `summarize_cluster_issues()` | `ai_tools/report.py` | Summarizes multiple issues into an executive cluster report |

All four methods have a graceful fallback — they return a placeholder message if no API key is configured — so the server will not crash. However, the output becomes essentially empty.

---

## Tool-by-Tool Impact

### Tools That Work Fully Without Gemini (~15 tools)

These are pure `kubectl` wrappers. No LLM involvement at all.

| Tool | What It Does |
|---|---|
| `get_pods` | List pods in a namespace |
| `get_pod_logs` | Fetch container logs |
| `get_events` | Fetch namespace events |
| `get_deployment` | Deployment status |
| `get_service` | Service details |
| `get_endpoints` | Endpoint readiness |
| `find_workload` | Search for a workload across namespaces |
| `get_rollout_status` | Deployment rollout progress |
| `list_contexts` | List kubeconfig contexts |
| `switch_context` | Switch active cluster |
| `exec_command` | Run a command inside a pod |
| `delete_pod` | Delete a pod |
| `restart_deployment` | Rollout restart a deployment |
| `scale_deployment` | Scale replica count |
| `patch_resource` | Patch any resource with JSON |

### Tools That Partially Work Without Gemini (~2 tools)

| Tool | What Still Works | What Is Lost |
|---|---|---|
| `get_fix_commands` | 11 curated playbooks covering the most common categories (`pod_crashloop`, `pod_oom`, `pod_image`, `pod_pending`, `pod_evicted`, `rbac`, `networking`, `storage`, `helm_type_error`, `deployment_stuck`, `node`) | For unknown/uncategorized errors, the tool falls back to Gemini. Without it, the response is an empty "no playbook found" message. |
| `investigate_pod` | Full data gathering works — describe, logs, events, classification are all kubectl-only | The `ai` block in the result (root cause + fix commands tailored to the live pod) is skipped. You get raw data but no interpretation. |

### Tools That Become Non-Functional Without Gemini (~4 tools)

| Tool | Output Without Gemini |
|---|---|
| `analyze_error` | Returns only the detected error category string + `"Add GEMINI_API_KEY to .env to enable AI analysis."` No root cause, no solution, no steps, no commands. |
| `generate_runbook` | Returns `"LLM not configured."` — completely empty. |
| `cluster_report` | Returns the raw events text with no summary or prioritization. |
| `error_summary` | Returns a list of errors with no interpretation or patterns identified. |

---

## Is Full Removal a Good Approach?

### When It Is Fine

- Your team primarily uses the tool as a **kubectl UI** — checking pod status, viewing logs, listing events, investigating pods interactively.
- You have DevOps-experienced users who can interpret raw `kubectl` output themselves.
- Data privacy is a concern and you don't want error logs leaving your network.

### When It Is Not Fine

- The primary use case is **"paste an error and get a fix"** — this is 100% Gemini. Without it, `analyze_error` returns nothing useful.
- Non-DevOps users (developers, QA) are the target audience — they need the interpretation layer, not raw `kubectl` data.
- You want runbook generation or cluster health summaries — both are entirely dependent on the LLM.

### Problems With Full Removal

1. **Chat routing degrades silently.** The keyword fallback is brittle. Messages like "my service isn't reachable from other pods" or "deployment keeps rolling back" won't match any keyword pattern and will fall through to `analyze_error`, which then also returns nothing useful.

2. **The tool's main differentiator disappears.** The value proposition over a plain `kubectl` terminal is the AI layer. Without it, the UI is essentially a click-through wrapper around commands a DevOps engineer already runs manually.

3. **`generate_runbook` becomes a dead button.** There is no rule-based fallback — the tool is purely generative.

4. **Error pattern recognition is lost.** `cluster_report` and `error_summary` exist to identify recurring patterns across many events. Without the LLM, they return raw data with no insight.

---

## Effort Estimate to Remove Gemini

| Area | Effort | Notes |
|---|---|---|
| Chat intent router | 0 min | Keyword fallback already in place |
| `investigate_pod` AI layer | 10 min | Change `use_ai=True` default to `False` in `wrappers.py` and `schemas.py` |
| `get_fix_commands` curated path | 0 min | The 11 playbooks already work independently |
| `analyze_error` graceful stub | 30 min | Replace the "add API key" message with a more helpful rule-based response for known categories |
| `generate_runbook`, `cluster_report`, `error_summary` | 20 min | Add clear "not available without AI" messages with suggestions |
| **Total** | **~1 hour** | But 4-5 tools become non-functional |

---

## Recommended Middle Path: Swap Gemini for a Local LLM (Ollama)

If the concern is **data privacy** (error logs leaving your network) or **API costs**, the better solution is to replace Gemini with a locally-hosted LLM rather than removing the AI layer entirely.

[Ollama](https://ollama.com) runs models like `llama3`, `mistral`, or `deepseek-coder` entirely on your own machine or server with no external API calls.

### What Would Change

Only **one file** needs to change: `mcp/services/llm_service.py`

The rest of the stack — `chat.py`, `ai_tools/`, `k8s/wrappers.py` — stays identical.

### Approximate Code Change

```python
# Current (Gemini)
from google import genai
client = genai.Client(api_key=settings.gemini_api_key)
response = client.models.generate_content(model="gemini-3.1-flash-lite", contents=prompt, ...)

# Replaced with (Ollama)
import requests
response = requests.post("http://localhost:11434/api/generate", json={
    "model": "llama3",
    "prompt": prompt,
    "stream": False,
})
text = response.json()["response"]
```

### Ollama Setup (Local)

```bash
# Install Ollama
brew install ollama          # macOS

# Pull a model (one-time download)
ollama pull llama3           # ~4 GB, good general purpose
ollama pull deepseek-coder   # better for code/kubectl analysis

# Start Ollama server (runs on localhost:11434)
ollama serve
```

### Tradeoffs vs Gemini

| | Gemini (current) | Ollama (local) |
|---|---|---|
| Data privacy | Sends data to Google | Fully local, no external calls |
| Cost | Free tier, then pay-per-token | Free (runs on your hardware) |
| Response quality | Very high (frontier model) | Good for technical tasks, slightly lower |
| Setup complexity | Just an API key | Install Ollama + download model (~4 GB) |
| Latency | ~2-4 seconds (network) | ~5-15 seconds (CPU) or ~1-3 sec (GPU) |
| Hardware requirements | None | 8 GB RAM minimum; 16 GB recommended |

### Effort to Implement

~2 hours to swap `llm_service.py` to use Ollama's REST API, add `OLLAMA_URL` and `OLLAMA_MODEL` env vars to `.env.example`, and test all four methods (`analyze`, `analyze_live_investigation`, `generate_runbook`, `summarize_cluster_issues`).

---

## Summary

| Option | Effort | AI Features | Data Privacy |
|---|---|---|---|
| Keep Gemini (current) | 0 | Full | Data sent to Google |
| Remove Gemini entirely | ~1 hour | Lost for 4-5 tools | Full |
| Swap to Ollama (local LLM) | ~2 hours | Full | Full (local only) |

**Recommendation:** If data privacy is the concern, swap to Ollama. If you genuinely don't need AI analysis and only want a kubectl UI, a full removal is straightforward — but understand that the chat interface degrades significantly for non-technical users.
