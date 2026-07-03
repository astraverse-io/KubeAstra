# Unified Roadmap: From Alert Investigation to a Kubernetes Reliability Platform

**Status:** Planning · **Scope:** the merged `kubeastra-ai-assistant` (interactive assistant + MCP) and the integrated `alerts/` investigation engine.

This document synthesizes two inputs:

1. `intelligent_alert_manager_future_features.md` (the original future-features brainstorm).
2. A code-grounded analysis of the merged repo (what is actually built today).

Its job is to (a) stop us from rebuilding things that already exist, and (b) sequence the remaining work by leverage. Where a feature originated in the brainstorm it is tagged **[Doc §N]**; net-new items from the code analysis are tagged **[Analysis]**.

---

## 0. The framing: the incident lifecycle

The merge produced a system that **Detects** and **Diagnoses** but does not yet **Correlate**, **Remediate**, **Verify**, or **Learn**:

```
Detect ──▶ Correlate ──▶ Investigate ──▶ Diagnose ──▶ Remediate ──▶ Verify ──▶ Learn
 (built)    (gap)          (built)        (built)       (gap)        (gap)      (gap)
```

Most of the highest-value next features are *loop-closing*: they connect machinery that already exists in the two halves of the merge but was never wired together. The original alert-manager **diagnoses** (read-only); the assistant **acts** (write, with approval). The merge co-located them but did not connect them.

---

## 1. Already built — DO NOT rebuild (verified in code)

This is the most important section. Several items in the original brainstorm are already present and only need *wiring*, not *building*.

| Capability | Build state | Evidence in code | What's actually missing |
|---|---|---|---|
| **Write/remediation tools + approval** [Doc §1] | **Built** | `delete_pod`, `rollout_restart`, `scale_deployment` registered MCP tools (`mcp/tool_registry.py:1091+`); `ui/backend/routers/recovery.py` gates every write behind `confirm=True`; write wrappers in `k8s/wrappers.py` | The **investigation → remediation bridge** (auto-propose a plan from an RCA). The tools themselves exist. |
| **Incident memory (Qdrant)** [Doc §3] | **~80% built** | `QdrantSemanticMemoryRepository.store/search` (`alerts/repositories/qdrant.py`); `SemanticMemoryRepository` interface (`repositories/base.py`); engine already calls `semantic_memory.store(...)` (`orchestrator/engine.py:283`) and builds `SemanticIncidentRecord` via `_to_semantic_record` | Swap the `InMemorySemanticMemoryRepository` stub for the Qdrant impl in the orchestrator wiring; bootstrap an `incident_memory` collection; add a **search-at-start** step. |
| **k8sgpt integration** [Doc §4] | **Scaffolded** | references across `tool_registry.py`, `config/settings.py`, `k8s/wrappers.py`, `mcp_server/schemas.py`; helm `enableK8sgpt` flag in `values.yaml` | The **scheduler** that runs scans proactively. k8sgpt itself is wired. |
| **Multi-cluster** [Doc §5] | **Built (interactive side)** | `cluster_connections` table + per-session kubeconfig/context (`db.py:115`); context switching in `k8s/kubectl_runner.py` | Make **investigations** cluster-aware: carry `cluster_id` from webhook → orchestrator → tool dispatch. |
| **Templated PromQL/LogQL + evidence infra** [partial of Doc §6] | **Built (templated)** | `alerts/investigation/prometheus/queries/` (`query_templates.yaml`, `templates.py`, `catalog.py`); `infrastructure/prometheus.py`, `infrastructure/loki.py` | **Dynamic, LLM-generated** queries (see §4.3) — only the hardcoded templates exist today. |

> Takeaway: the brainstorm under-credits the codebase. Remediation, incident memory, k8sgpt, and multi-cluster are not greenfield — they are "connect the wires" tasks. Budget accordingly.

---

## 2. Phase 1 — Close the core loops (highest leverage)

### 1.1 Wire incident memory — the learning loop  [Doc §3 + Analysis]
**Why first:** lowest effort (≈80% built), no dependencies, and it immediately raises diagnosis quality — which makes the later remediation loop *safer*.
- Replace the `InMemorySemanticMemoryRepository` injection in `routers/alerts.py` with `QdrantSemanticMemoryRepository`.
- Bootstrap an `incident_memory` Qdrant collection (separate from the `runbook` RAG collection; reuse the existing `rag-bootstrap-job` pattern).
- Add a first orchestrator step: semantic search for similar past incidents; inject "last time this fired, cause was X, fix was Y" into the RCA prompt.
- **Done when:** a repeated alert pattern surfaces the prior incident's RCA in the new investigation.

### 1.2 RCA → approved remediation → verify — the resolution loop  [Doc §1 + Analysis]
**Why:** the single biggest product leap; reuses the *already-built* approval/write machinery.
- From a completed RCA, generate a **remediation plan**: an ordered list of existing write tools (`scale_deployment`, `rollout_restart`, `delete_pod`, future `rollout_undo`).
- Surface the plan in `/alerts` and chat with the existing **dry-run preview + Approve & Run** flow (reuse `recovery.py` `confirm=True` + execution-token mechanism).
- **Verify step [Analysis]:** after a remediation runs, re-execute the relevant evidence step and confirm the alert condition cleared; record the outcome on the investigation.
- **Done when:** an operator can go alert → RCA → one-click approved fix → confirmation it worked, with a full audit trail.

### 1.3 Alert correlation / incident grouping  [Analysis — absent from the brainstorm]
**Why:** real outages are alert *storms*; today each alert spawns a separate investigation. This is the #1 day-to-day noise problem.
- Group alerts into a single incident by fingerprint + time window + topology proximity (reuse the assistant's existing topology graph to find a shared upstream resource).
- One investigation per incident, not per alert.
- **Done when:** a storm of N related alerts produces 1 incident with N correlated signals.

---

## 3. Phase 2 — Trust & operability

### 2.1 RCA feedback loop  [Analysis]
Let users mark an RCA correct / partial / wrong — reuse the existing `feedback_events` infrastructure. Accepted RCAs become **high-confidence** incident-memory records (feeds 1.1); disagreements drive playbook/prompt tuning. This is the quality gate that makes incident memory trustworthy.

### 2.2 Durable async execution  [Analysis — architectural enabler]
Investigations currently run as in-process `BackgroundTasks` on single-replica SQLite. This is the prerequisite for running multi-step, approval-gated remediations (1.2) reliably at scale. Move to a real job queue/worker (arq/Celery, or a SQLite-backed job table) with retries, timeouts, concurrency control, and the orphan-sweep already in place.

### 2.3 ChatOps notifications  [Analysis]
The `notifications/channels.py` Slack/PagerDuty stubs exist but only `LoggingNotificationChannel` is wired. Post RCAs to Slack, allow **approve-remediation from Slack**, sync incident state to PagerDuty.

---

## 4. Phase 3 — "And probably more": reactive → continuous reliability

### 4.1 Proactive / scheduled health scans  [Doc §4]
A scheduled job runs playbooks + k8sgpt (already scaffolded) for a daily briefing: missing resource limits, orphaned resources, expiring TLS, node disk pressure, **deprecated-API checks before an upgrade**. Only the scheduler is new work.

### 4.2 "What changed?" — GitOps/CI-CD context  [Doc §2 + Analysis]
On alert, the playbook checks for deployments in the last N minutes (ArgoCD/Flux/GitHub Actions/GitLab CI). Turns "the pod is crashing" into "the pod is crashing because commit `a1b2c3d` 10 minutes ago changed the readiness probe — revert the ArgoCD app?" Highest-signal single addition to RCA quality.

### 4.3 Dynamic PromQL/LogQL generation  [Doc §6 — genuinely net-new]
Give the agent `execute_promql` / `execute_logql` tools so it can write queries on the fly (not just run templates): on `OOMKilled`, generate a PromQL query plotting that pod's memory over 30 min and summarize *when* the spike happened. Builds on the existing Prometheus/Loki infra (§1), adds an LLM-authored query step + a safe execution wrapper.

### 4.4 Runbook flywheel  [Analysis]
When an investigation + remediation succeeds, auto-draft/update a runbook into the existing Qdrant **runbook** RAG collection. The tool writes its own runbooks → the next occurrence resolves faster. Self-reinforcing with the RAG already in production.

### 4.5 Multi-cluster fleet queries  [Doc §5 + Analysis]
Make investigations cluster-aware (carry `cluster_id`), then enable fleet-wide questions: "all OOMKills across clusters this week," "why does payment-service work in Staging but crash-loop in Prod — compare configmaps."

### 4.6 One-click postmortem  [Analysis]
Each investigation's `audit_log` already captures a timeline. Generate a blameless postmortem (timeline + RCA + remediation + impact) as markdown in one click.

---

## 5. Cross-cutting: safety as a first-class feature  [Analysis — critical, absent from brainstorm]

As the platform moves toward auto-remediation, the guardrail story is a deliverable, not an afterthought:
- **Policy-as-code:** which namespaces/actions are auto-approvable vs. require a human.
- **Blast-radius limits** on remediation plans (max pods affected, protected namespaces).
- **Full audit trail** (already partly present via `audit_log` + `feedback_events`).

This is what makes write-access enterprise-credible. It should land alongside Phase 1.2, not after.

---

## 6. Recommended sequence

```
1.1 Incident memory ──▶ 2.2 Durable execution ──▶ 1.2 Remediation + verify
        │                                                  │ (+ §5 safety/policy)
        ▼                                                  ▼
2.1 RCA feedback ─────────────────────────────────▶ 1.3 Correlation
                         then Phase 3: proactive scans, "what changed", dynamic queries,
                         runbook flywheel, fleet view, postmortems
```

**Start here:** **1.1 Incident memory.** It is the smallest, has no dependencies, is ~80% built, and makes every later step smarter. It is the natural next PR.

---

## 7. Overlap summary (this plan vs. the brainstorm)

| Theme | Brainstorm | This plan | Net change |
|---|---|---|---|
| Remediation + approval | §1 (proposed as new) | 1.2 | **Reclassified: mostly built; bridge only** |
| Incident memory | §3 | 1.1 | **Reclassified: ~80% built; wire only** |
| Proactive scans / k8sgpt | §4 | 4.1 | **Reclassified: scaffolded; scheduler only** |
| Multi-cluster | §5 | 4.5 | **Reclassified: interactive built; investigations only** |
| GitOps/CI-CD context | §2 | 4.2 | Same intent |
| Dynamic PromQL/LogQL | §6 | 4.3 | Kept as net-new |
| Alert correlation | — | 1.3 | **Added** |
| RCA feedback loop | — | 2.1 | **Added** |
| Durable execution | — | 2.2 | **Added (enabler)** |
| ChatOps | — | 2.3 | **Added** |
| Runbook flywheel | — | 4.4 | **Added** |
| Postmortems | — | 4.6 | **Added** |
| Safety / policy-as-code | — | §5 | **Added (critical)** |
