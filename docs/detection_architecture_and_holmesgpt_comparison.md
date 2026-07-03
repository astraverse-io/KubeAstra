# Detection Architecture, Multi-Cluster Strategy & HolmesGPT Comparison

**Audience:** engineering team reviewing the current system and planning next steps.
**Status:** working document for a team review session.
**Scope:** how detection works *today* in the merged `kubeastra-ai-assistant` (assistant + MCP + integrated `alerts/` engine), how it does/doesn't scale to many clusters, how it compares to HolmesGPT, and the decisions we need to make.

> Companion doc: [`unified_investigation_platform_roadmap.md`](./unified_investigation_platform_roadmap.md) holds the full feature backlog. This doc explains the *detection/architecture* foundation those features build on.

---

## Table of contents
1. [The mental model: detect vs. investigate](#1-the-mental-model-detect-vs-investigate)
2. [How detection works today (step by step)](#2-how-detection-works-today-step-by-step)
3. [Multi-cluster: can one agent cover everything?](#3-multi-cluster-can-one-agent-cover-everything)
4. [Deployment topologies (with trade-offs)](#4-deployment-topologies-with-trade-offs)
5. [What we have today (honest capability inventory)](#5-what-we-have-today-honest-capability-inventory)
6. [HolmesGPT comparison](#6-holmesgpt-comparison)
7. [Decisions we need to make](#7-decisions-we-need-to-make)
8. [Appendix: file map & glossary](#8-appendix-file-map--glossary)

---

## 1. The mental model: detect vs. investigate

The most important thing to internalize: **this system does not scan or poll clusters for problems. It is a webhook receiver.**

Two distinct phases, with very different properties:

| Phase | Who does it | Mechanism | Multi-cluster difficulty |
|---|---|---|---|
| **Detection** | The cluster's *existing* monitoring (Prometheus rules, Grafana, Loki) | **Push** — Alertmanager POSTs to our webhook | **Easy** — push centralizes naturally |
| **Investigation** | Our agent | **Pull** — runs `kubectl`/queries *against* the cluster | **Hard** — needs network reach + credentials to each cluster |

"Detection," from our agent's perspective, simply means **"an alert arrived."** The intelligence we add happens *after*: gather evidence, reason, produce a root-cause analysis (RCA), and (future) remediate.

This distinction drives every multi-cluster decision below.

---

## 2. How detection works today (step by step)

### 2.1 The flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  IN-CLUSTER (per monitored cluster)                                          │
│                                                                              │
│   Prometheus rule fires ──▶ Alertmanager ──┐                                 │
│   (e.g. KubePodCrashLooping)               │  webhook_config (HTTP POST)     │
└────────────────────────────────────────────┼────────────────────────────────┘
                                              │  Authorization: Bearer <token>
                                              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  THE AGENT (kubeastra-ai-assistant backend)                                 │
│                                                                              │
│  POST /api/v1/alerts/webhook                                                  │
│    1. Token check        → _verify_webhook_token (ALERT_WEBHOOK_TOKEN)        │
│    2. Normalize          → normalize_alert_payload (detect_source + parse)    │
│    3. Persist            → SqliteInvestigationRepository (status=received)    │
│    4. Respond 200        → returns investigation_ids immediately             │
│    5. Background task    → orchestrate_investigation(...)                     │
│         ├─ classify alert → pick playbook                                     │
│         ├─ run playbook   → MCP tools (kubectl) gather read-only evidence     │
│         ├─ LLM RCA        → services.llm                                      │
│         └─ status=completed / failed                                          │
│                                                                              │
│  Stored in SQLite `investigations`: JSON doc + indexed                        │
│  (namespace, severity, source, status, created_at)                           │
│                                                                              │
│  Exposed to the chat agent via MCP tools:                                     │
│    get_recent_alerts(...) · get_investigation_details(id)                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 The webhook contract

- **Endpoint:** `POST /api/v1/alerts/webhook` (`ui/backend/routers/alerts.py`).
- **Auth:** machine-to-machine. The endpoint is exempt from interactive user-session auth (`auth.is_public_path`) and instead validated by a shared bearer token `ALERT_WEBHOOK_TOKEN` (constant-time compare). If the token is unset, the webhook is open (dev only).
- **Sources supported:** Alertmanager, Grafana, Loki, and a generic/unknown fallback. `detect_source()` classifies the payload; any payload containing an `alerts` list is treated as Alertmanager-format.
- **Response:** `202`-style immediate `200` with `investigation_ids`; the investigation runs asynchronously in the background.

### 2.3 What gets captured (and how cluster identity flows)

`_normalize_alertmanager` copies **all** alert labels and annotations into the stored alert. This matters for multi-cluster: if each cluster's Prometheus sets an `external_labels: { cluster: <name> }`, that `cluster` label **already arrives and is already stored** in the alert document today — we just don't yet *route* on it (see §3).

Indexed columns for fast filtering: `namespace`, `severity`, `source`, `status`, `created_at`. (Cluster is currently in the JSON document/labels, not an indexed column.)

### 2.4 Alertmanager configuration (per cluster)

```yaml
# In each cluster's Alertmanager config
receivers:
  - name: ai-agent
    webhook_configs:
      - url: https://agent.example.com/api/v1/alerts/webhook
        send_resolved: true
        http_config:
          authorization:
            type: Bearer
            credentials: <ALERT_WEBHOOK_TOKEN>
route:
  receiver: ai-agent

# In each cluster's Prometheus (so alerts carry cluster identity)
global:
  external_labels:
    cluster: prod-eu-1
```

That is the **entire** detection setup per cluster: one receiver + one external label.

### 2.5 Investigation status lifecycle

`RECEIVED → CLASSIFIED → RUNNING → COMPLETED | FAILED`. On startup, a sweep flips any orphaned `RUNNING` rows to `FAILED` (protects against restarts mid-investigation). Investigations run as in-process FastAPI `BackgroundTasks`; the backend is intended to run at `replicas: 1` because state lives in SQLite on a PVC.

---

## 3. Multi-cluster: can one agent cover everything?

**For detection: yes.** Because detection is push-based, a single agent deployment can receive alerts from any number of clusters — point every cluster's Alertmanager at the same webhook URL. You do **not** need one agent per cluster to *detect*.

**For investigation: it depends**, and this is the real architectural question.

When an alert from *Cluster B* arrives, the agent must run `kubectl` **against Cluster B** to gather evidence. That requires, for Cluster B:
1. **Network reachability** to its API server.
2. **Credentials** (kubeconfig / ServiceAccount token).

### Current state vs. gap

| Capability | State today | Evidence |
|---|---|---|
| Receive alerts from many clusters | ✅ Works (central webhook) | `routers/alerts.py` |
| Cluster identity arrives with the alert | ✅ Captured (if `external_labels` set) | `_normalize_alertmanager` preserves labels |
| Investigate the cluster the backend is pointed at | ✅ Works | MCP `kubectl_runner` uses configured `KUBECONFIG` |
| **Investigate the *correct* cluster per alert** | ❌ **Gap** | Orchestrator does not read the alert's `cluster` label and switch kubeconfig/context |
| Manage multiple kubeconfigs/contexts (interactive) | ✅ Built | `cluster_connections` table (`db.py`), context switching in `kubectl_runner.py` |
| In-cluster least-privilege investigation | ✅ Built | Helm `ClusterRole` + ServiceAccount |

**Bottom line:** the *plumbing* for multi-cluster exists (multi-kubeconfig support, per-cluster RBAC). The missing wire is **cluster-aware investigation routing**: `webhook(cluster label) → orchestrator → select kubeconfig/context → dispatch tools`. This is tracked in the roadmap (§4.5 / multi-cluster).

---

## 4. Deployment topologies (with trade-offs)

### Topology A — Central hub
One agent holds kubeconfigs for **all** clusters and reaches into each over the network.

```
 Cluster A ─┐
 Cluster B ─┼─ alerts ─▶  ONE agent  ─ kubectl over network ─▶  back into A/B/C
 Cluster C ─┘            (holds all kubeconfigs)
```
- **Pros:** single pane of glass; fleet-wide correlation & incident memory are natural; one thing to operate.
- **Cons:** the hub must **reach every cluster's API server** (hard for private/isolated clusters); storing all clusters' credentials in one place is a large **security blast radius**; hub is a single point of failure.
- **Good when:** clusters are network-reachable from a management cluster (peered VPCs, same network).

### Topology B — Agent per cluster
Each cluster runs its own agent; investigates **locally** with an in-cluster ServiceAccount.

```
 Cluster A:  Alertmanager ─▶ Agent A (in-cluster SA) ─▶ kubectl (local)
 Cluster B:  Alertmanager ─▶ Agent B (in-cluster SA) ─▶ kubectl (local)
```
- **Pros:** no cross-cluster networking or credential-sharing problems; least-privilege; scales naturally; investigation is local and fast.
- **Cons:** N deployments to operate; **no built-in fleet view / cross-cluster correlation / shared memory** unless you add a central layer.
- **Good when:** clusters are isolated / have private API servers (typical **kubeadm in separate environments**). This is what the current Helm chart is built for.

### Topology C — Hybrid (per-cluster collector + central brain)
A lightweight per-cluster component does **local** evidence-gathering (solves reach + creds) and forwards normalized findings to **one central brain** that does RCA, memory, correlation, and the UI.

```
 Cluster A ─ collector ─┐
 Cluster B ─ collector ─┼─ findings ─▶  Central brain (RCA, memory, UI, correlation)
 Cluster C ─ collector ─┘
```
- **Pros:** local reach **and** a single pane; best long-term shape for a fleet.
- **Cons:** most to build (split responsibilities, a forwarding protocol).
- **Good when:** many clusters + you want fleet-wide intelligence. (This is, notably, the shape Robusta/HolmesGPT uses — see §6.)

### Recommendation for "tons of kubeadm clusters in different environments"
Private kubeadm API servers usually **break Topology A** (unreachable from a hub) and concentrate too many credentials. Start at **Topology B** (the chart already supports it via in-cluster ServiceAccount), and evolve toward **Topology C** by forwarding each cluster's investigations to a central instance for the unified view, correlation, and incident memory. Detection can be centralized immediately regardless of which we choose.

---

## 5. What we have today (honest capability inventory)

| Area | Status | Notes |
|---|---|---|
| Alert ingestion (Alertmanager/Grafana/Loki) | ✅ Built | Webhook + normalization + token auth |
| Deterministic playbook investigation | ✅ Built | `alerts/playbooks/` + `data/playbooks/generic.yaml` |
| Read-only evidence via unified MCP tools | ✅ Built | `tool_registry.resolve_tool` (kubectl wrappers) |
| LLM RCA | ✅ Built | `services.llm` (Gemini; provider-pluggable) |
| Persistence (single replayable doc) | ✅ Built | SQLite `investigations` (JSON + indexed fields) |
| Agent tools to query alerts in chat | ✅ Built | `get_recent_alerts`, `get_investigation_details` |
| Interactive chat + ReAct agent | ✅ Built | `react.py` (agentic, function-calling style) |
| Approval-gated write/remediation tools | ✅ Built (not bridged) | `recovery.py` (`confirm=True`), `scale_deployment`/`delete_pod`/`rollout_restart` registered |
| Multi-kubeconfig management (interactive) | ✅ Built | `cluster_connections` |
| Per-cluster RBAC for investigation | ✅ Built | Helm `ClusterRole` + SA |
| **Incident memory (semantic recall)** | ⚠️ ~80% | `QdrantSemanticMemoryRepository` exists; engine calls `.store()`; **stub-wired to in-memory** |
| **Cluster-aware investigation routing** | ❌ Gap | alert `cluster` label not used to pick kubeconfig |
| Alert correlation / storm grouping | ❌ Gap | one investigation per alert today |
| RCA → remediation bridge + verify | ❌ Gap | tools exist; not driven from RCA |
| Durable async execution (queue/worker) | ❌ Gap | in-process `BackgroundTasks`, single replica |
| Proactive/scheduled scans | ❌ Gap | k8sgpt scaffolded; no scheduler |
| Notifications (Slack/PagerDuty) | ❌ Gap | channel stubs exist; only logging wired |
| Central fleet view / cross-cluster correlation | ❌ Gap | needs Topology C |

---

## 6. HolmesGPT comparison

> ⚠️ **Verification note:** the HolmesGPT details below reflect general knowledge as of **early 2026**. HolmesGPT/Robusta evolve quickly. **Confirm each row against the current docs before treating this as authoritative** — a checklist is at the end of this section. The statements about *our* system are code-verified.

**What HolmesGPT is:** Robusta's open-source "AI on-call / alert investigation" agent. Same problem space as us: an alert fires, an LLM investigates using read-only data sources and produces an RCA.

### Same family

- **Alert-driven detection** (Alertmanager → investigation); not polling. Same model as §2.
- **LLM-based RCA** over **read-only** evidence from multiple sources (kubectl, Prometheus, Loki, Grafana, etc.).
- Produces findings/RCA, not just a forwarded alert.

### Key differences

| Dimension | HolmesGPT | Ours (merged) |
|---|---|---|
| **Investigation control flow** | **Fully agentic** — LLM dynamically chooses tools (function-calling/ReAct) and loops until satisfied | **Hybrid** — alert side is **deterministic playbooks** + bounded LLM for RCA; interactive side (`react.py`) **is** agentic |
| **Tool abstraction** | "**Toolsets**" (configurable collections the LLM draws from) | **MCP-native** tool registry (reusable by external IDE agents) |
| **Determinism / auditability** | Flexible, less predictable per run | Playbook path is reproducible & auditable (you know exactly what evidence was collected) |
| **Remediation** | Primarily **read-only** (adding more action/runbook capability over time) | **Approval-gated write tools already present** (`recovery.py`); closing detect→fix loop is more native |
| **Deployment / multi-cluster** | Robusta runs a **per-cluster runner agent** → optional central **SaaS** UI (≈ Topology C) | Built for **per-cluster in-cluster SA** (Topology B); central aggregation UI not built yet |
| **Memory / "seen this before"** | Runbooks + Robusta platform history | Qdrant **incident memory** (scaffolded) |
| **Hosting** | OSS engine; commonly used with Robusta SaaS | Fully self-hosted single stack |

### Honest read for the team
We have **not** reinvented HolmesGPT. Same problem + same detection model, different philosophy:
- **Determinism + auditability** (our playbooks) vs. **flexibility** (their agentic toolsets).
- **MCP-native** tooling (IDE/agent-reusable).
- **Built-in approval-gated remediation** (a genuine differentiator if we build the remediation phase).
- Robusta's **per-cluster runner → central SaaS** is independent validation of the Topology B→C path recommended in §4.

### Strategic options w.r.t. HolmesGPT
1. **Differentiate** — lean into MCP-native + deterministic-auditable + remediation; position as the "acts, not just diagnoses" tool.
2. **Integrate** — HolmesGPT supports being driven via MCP/tools; we could call it as one investigation backend while keeping our orchestration, UI, memory, and remediation.
3. **Adopt + extend** — use HolmesGPT for investigation and build our remediation/memory/correlation around it.
(We should pick a stance deliberately — see §7.)

### Verification checklist (do before finalizing)
- [ ] Is HolmesGPT investigation still fully agentic (toolsets + function-calling)?
- [ ] Current state of **write/remediation** actions in HolmesGPT?
- [ ] Does it expose/consume **MCP** (so we could integrate rather than rebuild)?
- [ ] Confirm Robusta deployment topology (per-cluster runner + SaaS) and whether a fully self-hosted central exists.
- [ ] License terms for OSS engine vs. SaaS features.

---

## 7. Decisions we need to make

1. **Deployment topology** (§4): central hub, per-cluster, or hybrid? Drives almost everything else. *Recommendation: per-cluster (B) now, evolve to hybrid (C).*
2. **Cluster-aware investigation** (§3): commit to carrying `cluster_id` from alert → orchestrator → tool dispatch. *Small, unblocks real multi-cluster.*
3. **Build vs. integrate vs. adopt HolmesGPT** (§6): pick a stance before investing in our own agentic investigation engine.
4. **Incident memory** (§5): wire the existing Qdrant repo now (≈80% done, no dependencies, improves every RCA).
5. **Remediation posture**: do we bridge RCA → approved write tools, and what's the safety/policy model (namespaces allowed, blast-radius limits, approval gates)?
6. **Durability**: when do we move investigations off in-process `BackgroundTasks` to a real queue/worker (prerequisite for safe remediation at scale)?

Suggested first concrete steps (lowest risk, highest unblock):
1. Wire incident memory (Qdrant).
2. Add `cluster_id` plumbing (cluster-aware investigation).
3. Decide the HolmesGPT stance (so #4+ of the roadmap aren't wasted effort).

---

## 8. Appendix: file map & glossary

### Key files
| Concern | Path |
|---|---|
| Webhook + orchestration trigger | `ui/backend/routers/alerts.py` |
| Webhook auth exemption | `ui/backend/auth.py` (`is_public_path`) |
| Alert normalization / source detection | `mcp/alerts/domain/normalization.py` |
| Investigation orchestrator | `mcp/alerts/orchestrator/engine.py` |
| Playbooks | `mcp/alerts/playbooks/`, `mcp/data/playbooks/` |
| Unified tool registry (kubectl, etc.) | `mcp/tool_registry.py` |
| LLM service | `mcp/services/llm_service.py`, `services/llm/` |
| Persistence (SQLite) | `ui/backend/db.py` (`investigations` table, `SqliteInvestigationRepository`) |
| Agent tools (chat) | `mcp/alerts/api/mcp_tools.py` |
| Incident memory (scaffolded) | `mcp/alerts/repositories/qdrant.py` |
| Write/remediation | `ui/backend/routers/recovery.py`, `k8s/wrappers.py` |
| Multi-cluster (kubeconfig) | `ui/backend/db.py` (`cluster_connections`), `mcp/k8s/kubectl_runner.py` |
| Helm chart | `helm/kubeastra/` |

### Glossary
- **Detection** — receiving an alert via webhook (the monitoring stack does the actual noticing).
- **Investigation** — gathering read-only evidence from a cluster and producing an RCA.
- **Playbook** — code/YAML-defined deterministic investigation steps.
- **Toolset** (HolmesGPT) — a configurable collection of tools the LLM may call.
- **MCP** — Model Context Protocol; our tool interface, reusable by external agents/IDEs.
- **Topology A/B/C** — central hub / agent-per-cluster / hybrid collector+brain.
- **RCA** — root-cause analysis (the investigation's output document).
- **`external_labels`** — Prometheus labels (e.g. `cluster`) stamped on every alert; how cluster identity reaches us.
