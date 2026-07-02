# Plan - Deterministic Kubernetes Health Analyzer

## Purpose

Build a first-party Kubernetes analyzer that provides the useful parts of `k8sgpt` without adding an external binary dependency or weakening the assistant's reliability model.

The analyzer should inspect live Kubernetes evidence collected through the existing request-scoped kubectl runner, apply deterministic rules, return structured findings, and let the LLM summarize those findings with citations to exact resources/events/log snippets.

Target architecture:

```text
User prompt
  -> ReAct/tool routing
  -> kubectl wrappers using local/session/SSH runner
  -> normalized evidence
  -> deterministic analyzer rules
  -> structured findings
  -> LLM summary grounded in findings and optional RAG/runbooks
```

## Goals

- Replace the need for `k8sgpt` in normal investigations.
- Keep all analysis request-scoped, so local kubeconfig, uploaded kubeconfig, and SSH sessions work consistently.
- Make common Kubernetes failures detectable without relying on the LLM to infer everything from raw JSON.
- Return structured, testable findings with severity, evidence, impacted resource, probable cause, and suggested next checks.
- Preserve low latency by running a focused analyzer only when the prompt or ReAct path needs it.
- Keep the LLM as a summarizer/reasoner, not the sole source of truth.

## Non-Goals

- Do not build a full policy engine in the first pass.
- Do not replace all existing wrappers or ReAct behavior.
- Do not make destructive remediation automatic.
- Do not require cluster-wide scans for every prompt.
- Do not add `k8sgpt` as a hard dependency.

## Proposed Module Layout

```text
mcp/
  k8s/
    analyzer/
      __init__.py
      models.py          # Finding, EvidenceRef, Severity, AnalyzerResult
      collectors.py      # Evidence gathering helpers
      rules.py           # Deterministic rule functions
      engine.py          # Runs selected rules over evidence
      formatters.py      # Compact output for ReAct / UI
  tests/
    test_health_analyzer_*.py
```

Optional later:

```text
ui/backend/
  tests/
    test_health_analyzer_chat_integration.py
```

## Data Model

Use a small structured finding model. Keep it stable so the UI, ReAct loop, and tests can depend on it.

```python
class EvidenceRef:
    source: str          # pod_status | event | log | node_condition | service | pvc
    namespace: str | None
    kind: str
    name: str
    field: str | None
    value: str | None
    message: str | None

class Finding:
    rule_id: str
    severity: str        # critical | warning | info
    category: str        # pod | node | scheduling | service | storage | rollout | resource
    title: str
    summary: str
    namespace: str | None
    resource_kind: str | None
    resource_name: str | None
    evidence: list[EvidenceRef]
    probable_cause: str | None
    recommended_next_steps: list[str]
    suggested_read_only_commands: list[str]
```

Rules should return findings, not prose paragraphs. The LLM can turn findings into a user-facing response later.

## Phase 1 - Foundation

Create the analyzer package and models.

Implementation:

- Add `k8s/analyzer/models.py`.
- Add `k8s/analyzer/engine.py`.
- Add `run_health_analyzer(scope)` where scope can be:
  - `cluster`
  - `namespace`
  - `pod`
  - `workload`
  - `node`
- Keep evidence collection small and scoped. Avoid cluster-wide pod/event scans unless the user asks for cluster or namespace health.

Initial analyzer result shape:

```python
{
  "scope": {"type": "namespace", "namespace": "default"},
  "finding_count": 3,
  "findings": [...],
  "summary": {
    "critical": 1,
    "warning": 2,
    "info": 0
  }
}
```

Acceptance criteria:

- Analyzer can run on fake Kubernetes JSON in tests.
- No LLM calls inside the analyzer.
- No direct subprocess calls. Use existing wrappers or `get_runner()`.

## Phase 2 - Pod Failure Rules

Implement the highest-value pod rules first.

Rules:

- `pod_waiting_crashloopbackoff`
  - Detect container waiting reason `CrashLoopBackOff`.
  - Include restart count, last termination reason, and recent warning events.
- `pod_waiting_image_pull`
  - Detect `ImagePullBackOff` and `ErrImagePull`.
  - Include image name and pull-related event messages.
- `pod_create_container_config_error`
  - Detect `CreateContainerConfigError`.
  - Correlate common event text for missing ConfigMap, Secret, key, or invalid env reference.
- `pod_oomkilled`
  - Detect last terminated reason `OOMKilled`.
  - Include memory requests/limits if present.
- `pod_high_restart_count`
  - Warn when restart count exceeds a threshold.
  - Threshold should be configurable, default 5.

Evidence inputs:

- Pod JSON.
- Pod events.
- Optional recent logs for specific pod investigations.

Acceptance criteria:

- Each rule has unit tests with synthetic pod/event JSON.
- Findings include exact pod/container names.
- No rule emits a finding without evidence.

## Phase 3 - Scheduling and Node Rules

Rules:

- `pod_pending_failed_scheduling`
  - Detect Pending pods with `FailedScheduling` events.
  - Extract insufficient CPU/memory, taints, node affinity, topology spread, or PVC binding hints from event messages.
- `node_not_ready`
  - Detect node Ready condition not `True`.
- `node_pressure`
  - Detect `MemoryPressure`, `DiskPressure`, `PIDPressure`, `NetworkUnavailable`.
- `node_over_allocated`
  - Reuse the existing node allocation rollup.
  - Warn when CPU or memory requests exceed a configurable percentage of allocatable.

Acceptance criteria:

- Node rules work with local and SSH runners.
- Ambiguous node names produce structured ambiguity, not a guessed result.
- Resource percentages match the existing `investigate_node` behavior.

## Phase 4 - Service, Endpoint, and Rollout Rules

Rules:

- `service_no_endpoints`
  - Detect services with zero ready endpoints.
  - Include selector and matching pod count if available.
- `service_selector_matches_no_pods`
  - Detect service selectors that match no pods.
- `deployment_unavailable_replicas`
  - Detect unavailable replicas.
  - Include desired/available/updated replica counts.
- `rollout_progress_deadline_exceeded`
  - Detect rollout conditions with `ProgressDeadlineExceeded`.
- `workload_pods_unhealthy`
  - For deployments/statefulsets/daemonsets, summarize unhealthy child pods.

Acceptance criteria:

- A service finding should explain whether the problem is selector mismatch, pods not ready, or no backing pods found.
- Rollout findings should include exact deployment names and conditions.

## Phase 5 - Storage and Mount Rules

Rules:

- `pvc_pending`
  - Detect PVCs stuck Pending.
  - Include storage class and requested size.
- `pod_mount_failure`
  - Detect mount/attach failures from events.
  - Include volume name and event message.
- `secret_or_configmap_missing`
  - Detect missing Secret/ConfigMap references from pod spec and events.

Acceptance criteria:

- Rules distinguish missing object references from runtime mount failures where possible.
- Findings include the referenced Secret/ConfigMap/PVC name.

## Phase 6 - Integration With Existing Tools

Add a new read-only tool:

```text
analyze_k8s_health(scope_type, namespace?, resource_name?, resource_kind?)
```

Tool behavior:

- For namespace health: collect pods, events, services, endpoints, deployments, PVCs in that namespace.
- For pod health: use existing pod investigation evidence where possible.
- For node health: use existing node investigation evidence.
- For cluster health: start with nodes, namespaces, warning events, and unhealthy pods. Keep output capped.

Registry integration:

- Add schema to `mcp_server/schemas.py`.
- Add handler in `tool_registry.py`.
- Expose to chat/react surfaces.
- Add to generated ReAct tool descriptions.

ReAct guidance:

- Use this tool when the user asks broad health questions:
  - "are there any issues?"
  - "cluster health"
  - "what is wrong in namespace X?"
  - "any warnings or failing pods?"
- Do not use it for simple list-only questions.

Acceptance criteria:

- The tool returns structured findings, not freeform LLM output.
- The final LLM answer cites finding titles and affected resources.

## Phase 7 - UI and Answer Quality

Backend:

- Ensure `suggested_actions` only includes read-only diagnostic commands by default.
- Keep destructive actions behind the existing confirmation flow.

Frontend:

- If useful, render findings as cards:
  - severity
  - title
  - resource
  - evidence
  - suggested next checks

Answer style:

- Start with the highest severity findings.
- Include "No critical findings detected" only when rules actually ran and found none.
- Avoid overclaiming root cause when evidence only supports a probable cause.

## Phase 8 - Telemetry and Tuning

Add counters/log events:

- `health_analyzer_started`
- `health_analyzer_completed`
- `health_analyzer_findings_count`
- `health_analyzer_rule_hit`
- `health_analyzer_rule_error`
- `health_analyzer_duration_ms`

Use telemetry to tune:

- Restart count thresholds.
- Resource allocation thresholds.
- Which rules are noisy.
- Which rules users find helpful.

## Testing Strategy

Unit tests:

- Rule tests with synthetic Kubernetes JSON.
- Memory/cpu parsing tests.
- Event correlation tests.
- Service selector matching tests.
- Rollout condition tests.

Integration tests:

- Tool registry dispatch tests.
- ReAct chooses analyzer for broad health prompt.
- Analyzer works under fake `KubectlRunner`.
- Analyzer handles empty namespaces gracefully.

Manual tests:

- `analyze namespace k8s-devops`
- `are there any issues in default?`
- `why are pods pending in namespace X?`
- `check node k8s-worker-01 health`
- `does service my-app have endpoints?`

## Reliability Principles

- Prefer structured evidence over prose.
- Prefer scoped collection over cluster-wide collection.
- Return "unknown / not enough evidence" rather than guessing.
- Never let the analyzer mutate cluster state.
- Never let the analyzer bypass request-scoped runner context.
- Keep LLM output grounded in analyzer findings and live kubectl evidence.

## Rollout Plan

1. Build rules behind the new `analyze_k8s_health` tool.
2. Keep existing ReAct behavior unchanged initially.
3. Manually call the tool on test clusters and validate findings.
4. Add ReAct guidance for broad health prompts.
5. Enable in internal beta.
6. Monitor telemetry and tune noisy rules.

## Deferred Ideas

- Rule configuration file for thresholds.
- Organization-specific platform rules.
- Finding suppression for known benign events.
- Historical trend comparison using session memory or Qdrant.
- Auto-generated runbook links per finding category.
- Optional UI filters by severity/category/namespace.

## Open Questions

- Should thresholds be global settings or namespace/team-specific?
- Should analyzer findings be stored in Qdrant/session memory for future retrieval?
- Should broad cluster analysis be rate-limited to avoid expensive all-namespace scans?
- Should the UI expose a "run health analyzer" button separate from chat?

## Recommendation

Implement this after the current reliability and context-isolation work is shipped to a small internal beta. This analyzer is valuable, but it should not block the immediate release unless users strongly need broad cluster health reports on day one.
