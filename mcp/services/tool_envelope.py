import logging
import json
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator


logger = logging.getLogger(__name__)

# Allowed values for the verdict field
VerdictBand = Literal["Healthy", "Unhealthy", "Degraded", "Unknown", "n/a"]

EVIDENCE_PRIORITY_RANKS = {
    "verified_root_cause": 100,
    "primary_failure": 80,
    "dependency_check": 70,
    "container_log_finding": 60,
    "secondary_issue": 40,
    "ai_advisory": 20,
}

EVIDENCE_PRIORITY_LABELS = {
    "verified_root_cause": "Verified root cause",
    "primary_failure": "Primary failure",
    "dependency_check": "Dependency check",
    "container_log_finding": "Container log finding",
    "secondary_issue": "Secondary issue",
    "ai_advisory": "AI advisory",
}


def _with_priority(item: Any, priority: str, **extra: Any) -> Dict[str, Any]:
    """Return a dict evidence item tagged with deterministic priority metadata."""
    tagged = dict(item) if isinstance(item, dict) else {"message": str(item)}
    tagged["evidence_priority"] = priority
    tagged["priority_rank"] = EVIDENCE_PRIORITY_RANKS.get(priority, 0)
    tagged["priority_label"] = EVIDENCE_PRIORITY_LABELS.get(priority, priority.replace("_", " ").title())
    tagged.update({k: v for k, v in extra.items() if v is not None})
    return tagged


def _event_last_timestamp(event: Dict[str, Any]) -> str:
    """Return the best available event timestamp for recency ordering."""
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    return str(
        event.get("last_timestamp")
        or event.get("lastTimestamp")
        or event.get("event_time")
        or event.get("eventTime")
        or metadata.get("creationTimestamp", "")
    )

class InventoryEvidence(BaseModel):
    """Tools that return lists/catalogs without health claims.
    Used by: get_namespaces, list_services, get_nodes, get_pods (listing mode).
    """
    type: Literal["inventory"] = "inventory"
    items: List[Dict[str, Any]]
    total_count: int
    filter_criteria: Dict[str, str] = Field(default_factory=dict)

class DiagnosticEvidence(BaseModel):
    """Tools that assess a specific workload's health.
    Used by: investigate_pod, investigate_workload, analyze_error.
    """
    type: Literal["diagnostic"] = "diagnostic"
    primary_target: Dict[str, Any]
    failure_modes: List[Dict[str, Any]]
    contributing_factors: List[Dict[str, Any]] = Field(default_factory=list)
    timeline: List[Dict[str, Any]] = Field(default_factory=list)

class StatusCheckEvidence(BaseModel):
    """Tools that check current state (rollouts, services).
    Used by: get_rollout_status, get_deployment, get_endpoints.
    """
    type: Literal["status_check"] = "status_check"
    target: Dict[str, Any]
    current_state: str
    desired_state: str
    drift: List[Dict[str, Any]] = Field(default_factory=list)
    last_transition: Optional[str] = None  # ISO timestamp

class LogAnalysisEvidence(BaseModel):
    """Tools that return time-series text data.
    Used by: get_pod_logs, get_events.
    """
    type: Literal["log_analysis"] = "log_analysis"
    source: Dict[str, Any]
    # Optional: not every log source carries usable timestamps. ``kubectl logs``
    # without ``--timestamps`` returns no time data; only set when we have
    # actual ISO timestamps. The narrate prompt is told to treat absent
    # time_range as "recency unknown" rather than inferring from "unknown".
    time_range: Optional[Dict[str, str]] = None  # {"start": ISO, "end": ISO}
    severity_counts: Dict[str, int]  # {"error": N, "warning": M, ...}
    top_messages: List[Dict[str, Any]]
    most_recent_critical: Optional[Dict[str, Any]] = None

# Discriminated union of the four evidence sub-types
EvidenceUnion = Union[
    InventoryEvidence,
    DiagnosticEvidence,
    StatusCheckEvidence,
    LogAnalysisEvidence
]

class ConfidenceSignals(BaseModel):
    """Signals used to score answer confidence and uncertainty."""
    events_analyzed: int = 0
    log_lines_seen: int = 0
    investigation_window_minutes: int = 0
    data_completeness: Literal["complete", "partial", "stale"] = "complete"

class ToolMeta(BaseModel):
    """Metadata describing the tool execution details."""
    tool: str
    params: Dict[str, Any]
    duration_ms: float = 0.0
    # Set by ``_dedupe_envelopes`` when this envelope's verdict or evidence
    # differs from an earlier run of the same (tool, params). Never set by
    # the producer — only by the dedup pass. Carrying this on ``meta``
    # instead of mutating ``params`` keeps params semantically clean (only
    # holds the values actually passed to the tool).
    revised_from_step: Optional[int] = None

class ToolEnvelope(BaseModel):
    """Uniform container wrapping every tool result."""
    verdict: VerdictBand
    evidence: EvidenceUnion = Field(..., discriminator="type")
    raw_excerpt: str = Field(default="", max_length=2048)
    confidence_signals: ConfidenceSignals = Field(default_factory=ConfidenceSignals)
    # Serialize as _meta in JSON but represent as meta in Python
    meta: ToolMeta = Field(..., alias="_meta")

    class Config:
        populate_by_name = True  # Pydantic v2 style for allow_population_by_field_name

    @field_validator("raw_excerpt", mode="before")
    @classmethod
    def truncate_raw(cls, v: Any) -> str:
        """Defensive backstop — truncates if a producer slipped past the
        ``_truncate_raw`` helper below. Producers should call ``_truncate_raw``
        directly so the truncation event is logged with the tool name.
        """
        if not isinstance(v, str):
            return ""
        if len(v) > 2048:
            logger.warning(
                "raw_excerpt truncated at envelope-constructor level "
                "(producer should have used _truncate_raw with tool context)"
            )
            return v[:2045] + "..."
        return v or ""


# ── Producer-side helpers ─────────────────────────────────────────────────────

def _truncate_raw(raw: Any, tool: str) -> str:
    """Truncate ``raw_excerpt`` to 2048 chars and log with tool context.

    Per the plan, truncation events are observability data — log the tool
    name so we can spot producers that need their summaries tightened.
    """
    if not isinstance(raw, str):
        return ""
    if len(raw) > 2048:
        logger.warning(
            "raw_excerpt truncated tool=%s original_len=%d",
            tool, len(raw),
        )
        return raw[:2045] + "..."
    return raw


# ── Conversion Helpers ────────────────────────────────────────────────────────

def make_pod_logs_envelope(result: Dict[str, Any], params: Dict[str, Any], duration_ms: float) -> ToolEnvelope:
    """Wraps get_pod_logs result in a ToolEnvelope."""
    success = result.get("success", False)
    logs = result.get("logs", "")
    stats = result.get("summary_stats") or {}
    
    # Calculate severity counts
    error_lines = stats.get("error_lines", 0)
    warn_lines = stats.get("warn_lines", 0)
    if not stats and logs:
        # Fallback keyword count if stats not present
        error_lines = sum(1 for line in logs.splitlines() if any(k in line.lower() for k in ["error", "fail", "exception"]))
        warn_lines = sum(1 for line in logs.splitlines() if "warn" in line.lower())

    # Determine verdict
    if not success:
        verdict = "Unknown"
    elif error_lines > 0:
        verdict = "Unhealthy"
    elif warn_lines > 0:
        verdict = "Degraded"
    else:
        verdict = "Healthy"

    # Evidence details
    evidence = LogAnalysisEvidence(
        source={
            "pod_name": result.get("pod_name", params.get("pod_name")),
            "namespace": result.get("namespace", params.get("namespace")),
            "container": result.get("container", params.get("container")),
            "previous": result.get("previous", params.get("previous", False))
        },
        # time_range left as None: kubectl logs without --timestamps gives us
        # no ISO data to populate. Once the wrapper passes --timestamps in
        # Sprint 2, extract min/max here.
        severity_counts={"error": error_lines, "warning": warn_lines},
        top_messages=[{"message": result.get("logs_summary", logs[:300])}] if (result.get("logs_summary") or logs) else []
    )

    # Raw excerpt — log with tool context if truncated.
    raw_for_excerpt = logs if success else result.get("error", "Failed to retrieve logs")
    raw_excerpt = _truncate_raw(raw_for_excerpt, "get_pod_logs")

    # Confidence signals
    data_completeness = "partial" if (result.get("truncated") or not success) else "complete"
    signals = ConfidenceSignals(
        log_lines_seen=stats.get("lines_in", logs.count("\n")),
        data_completeness=data_completeness
    )

    meta = ToolMeta(tool="get_pod_logs", params=params, duration_ms=duration_ms)

    return ToolEnvelope(
        verdict=verdict,
        evidence=evidence,
        raw_excerpt=raw_excerpt,
        confidence_signals=signals,
        _meta=meta
    )


def make_events_envelope(result: Dict[str, Any], params: Dict[str, Any], duration_ms: float) -> ToolEnvelope:
    """Wraps get_events result in a ToolEnvelope."""
    events = result.get("events", [])
    truncated = result.get("truncated", False)
    error = result.get("error")

    warning_events = [e for e in events if e.get("type") == "Warning"]
    warning_count = len(warning_events)
    normal_count = len(events) - warning_count

    # Determine verdict
    if error:
        verdict = "Unknown"
    elif warning_count > 0:
        verdict = "Unhealthy"
    else:
        verdict = "Healthy"

    # Time range — only populated when we have real timestamps; otherwise
    # left as None so the narrate prompt knows recency is unknown rather
    # than seeing the misleading literal string "unknown".
    timestamps = [_event_last_timestamp(e) for e in events if _event_last_timestamp(e)]
    time_range: Optional[Dict[str, str]] = None
    if timestamps:
        time_range = {"start": min(timestamps), "end": max(timestamps)}

    # Top messages (deduplicated by reason)
    seen_reasons = set()
    top_messages = []
    for e in warning_events + events:
        reason = e.get("reason")
        if reason not in seen_reasons:
            seen_reasons.add(reason)
            priority = "primary_failure" if e.get("type") == "Warning" else "secondary_issue"
            top_messages.append(_with_priority({
                "reason": reason,
                "message": e.get("message", ""),
                "count": e.get("count", 1),
                "object": f"{e.get('involved_object', {}).get('kind')}/{e.get('involved_object', {}).get('name')}"
            }, priority, source="event"))
            if len(top_messages) >= 8:
                break

    # Most recent critical event
    most_recent_warning = max(warning_events, key=_event_last_timestamp) if warning_events else None
    most_recent_critical = _with_priority(
        most_recent_warning,
        "primary_failure",
        source="event",
        priority_label="Most recent critical event",
    ) if most_recent_warning else None

    evidence = LogAnalysisEvidence(
        source={"namespace": result.get("namespace", params.get("namespace", "default"))},
        time_range=time_range,
        severity_counts={"warning": warning_count, "normal": normal_count},
        top_messages=top_messages,
        most_recent_critical=most_recent_critical
    )

    # Raw excerpt — log with tool context if truncated.
    if error:
        raw_excerpt = _truncate_raw(f"Error: {error}", "get_events")
    else:
        raw_excerpt = _truncate_raw(
            "\n".join(
                f"[{e.get('type')}] {e.get('reason')} - {e.get('message')}"
                for e in events[:15]
            ),
            "get_events",
        )

    signals = ConfidenceSignals(
        events_analyzed=result.get("original_count", len(events)),
        data_completeness="partial" if (truncated or error) else "complete"
    )

    meta = ToolMeta(tool="get_events", params=params, duration_ms=duration_ms)

    return ToolEnvelope(
        verdict=verdict,
        evidence=evidence,
        raw_excerpt=raw_excerpt,
        confidence_signals=signals,
        _meta=meta
    )


def make_investigate_pod_envelope(result: Dict[str, Any], params: Dict[str, Any], duration_ms: float) -> ToolEnvelope:
    """Wraps investigate_pod result in a ToolEnvelope."""
    success = result.get("success", True)
    classification = result.get("classification") or {}
    mode = classification.get("mode", "unknown")
    evidence_sum = result.get("evidence_summary") or {}

    # Determine verdict
    if not success or result.get("error"):
        verdict = "Unknown"
    elif mode in ("CrashLoopBackOff", "ImagePullBackOff"):
        verdict = "Unhealthy"
    elif mode == "Pending":
        verdict = "Degraded"
    else:
        # Check containers restart count
        pod_spec = result.get("pod_spec_summary") or {}
        has_restarts = False
        for c in pod_spec.get("containers", []) + pod_spec.get("init_containers", []):
            if c.get("restart_count", 0) > 0:
                has_restarts = True
                break
        verdict = "Degraded" if has_restarts else "Healthy"

    # Evidence mapping
    timeline = []
    for ev in evidence_sum.get("evidence", []):
        if isinstance(ev, dict):
            item = dict(ev)
        else:
            item = {"message": str(ev)}
        searchable = json.dumps(item, default=str).lower()
        if "oomkilled" in searchable or "out of memory" in searchable:
            timeline.append(_with_priority(item, "primary_failure", priority_label="Primary failure: OOMKilled"))
        else:
            timeline.append(_with_priority(item, "secondary_issue"))

    failure_modes = [_with_priority({
        "mode": mode,
        "container": classification.get("container"),
        "severity": "critical" if mode in ("CrashLoopBackOff", "ImagePullBackOff") else "warning"
    }, "primary_failure")] if mode != "unknown" else []

    suspected_root = evidence_sum.get("suspected_root_cause", "")
    suggested_fix = evidence_sum.get("suggested_fix", "")
    if suspected_root:
        failure_modes.insert(0, _with_priority({
            "type": "verified_root_cause",
            "source": "deterministic_investigation",
            "root_cause": suspected_root,
            "suggested_fix": suggested_fix,
            "severity": "critical" if verdict == "Unhealthy" else "warning",
        }, "verified_root_cause"))

    contributing_factors = []
    for item in evidence_sum.get("dependency_checks", []):
        contributing_factors.append(_with_priority(item, "dependency_check"))
    for finding in result.get("container_log_findings", []):
        if not isinstance(finding, dict):
            continue
        previous = finding.get("logs_previous") if isinstance(finding.get("logs_previous"), dict) else {}
        current = finding.get("logs_current") if isinstance(finding.get("logs_current"), dict) else {}
        excerpt = previous.get("excerpt") or current.get("excerpt") or ""
        contributing_factors.append(_with_priority({
            "container": finding.get("container"),
            "reason": finding.get("reason"),
            "restart_count": finding.get("restart_count"),
            "excerpt": str(excerpt)[:500],
        }, "container_log_finding"))
    for item in evidence_sum.get("secondary_issues", []):
        contributing_factors.append(_with_priority(item, "secondary_issue"))

    evidence = DiagnosticEvidence(
        primary_target={
            "pod_name": result.get("pod_name", params.get("pod_name")),
            "namespace": result.get("namespace", params.get("namespace")),
            "mode": mode,
            "container": classification.get("container")
        },
        failure_modes=failure_modes,
        contributing_factors=contributing_factors,
        timeline=timeline
    )

    # Raw excerpt (prefer suspected root cause, fall back to describe highlights)
    parts = []
    if suspected_root:
        parts.append(f"Suspected Root Cause: {suspected_root}")
    if suggested_fix:
        parts.append(f"Suggested Fix: {suggested_fix}")
    
    # Append first few lines of describe highlights if empty
    if not parts and "describe" in result:
        describe = result["describe"] or {}
        parts.append(str(describe.get("describe_summary") or describe.get("raw_output", ""))[:1000])

    raw_excerpt = _truncate_raw("\n\n".join(parts), "investigate_pod")

    # Confidence signals
    log_lines = 0
    for finding in result.get("container_log_findings", []):
        for log_key in ("logs_current", "logs_previous"):
            log_block = finding.get(log_key) or {}
            log_lines += str(log_block.get("logs", "")).count("\n")

    signals = ConfidenceSignals(
        events_analyzed=len(result.get("events", {}).get("events", [])),
        log_lines_seen=log_lines,
        data_completeness="complete" if success else "partial"
    )

    meta = ToolMeta(tool="investigate_pod", params=params, duration_ms=duration_ms)

    return ToolEnvelope(
        verdict=verdict,
        evidence=evidence,
        raw_excerpt=raw_excerpt,
        confidence_signals=signals,
        _meta=meta
    )


def make_generic_envelope(
    tool_name: str,
    result: Dict[str, Any],
    params: Dict[str, Any],
    duration_ms: float,
) -> ToolEnvelope:
    """Wraps any tool result in a ToolEnvelope dynamically based on the tool's classification."""
    # Check if tool_name is one of the existing specialized envelopes
    if tool_name == "get_pod_logs":
        return make_pod_logs_envelope(result, params, duration_ms)
    elif tool_name == "get_events":
        return make_events_envelope(result, params, duration_ms)
    elif tool_name == "investigate_pod":
        return make_investigate_pod_envelope(result, params, duration_ms)

    inventory_tools = {
        "find_workload", "get_namespaces", "get_nodes", "list_namespace_resources",
        "get_pods", "list_services", "list_kubeconfig_contexts", "kb_search", "get_fix_commands"
    }

    diagnostic_tools = {
        "investigate_workload", "analyze_namespace", "investigate_node",
        "analyze_error", "cluster_report", "error_summary", "generate_runbook"
    }

    status_check_tools = {
        "get_deployment", "get_service", "get_endpoints", "get_rollout_status",
        "get_resource_graph", "switch_kubeconfig_context"
    }

    meta = ToolMeta(tool=tool_name, params=params, duration_ms=duration_ms)
    confidence = ConfidenceSignals(data_completeness="complete")

    # If result is not a dictionary, wrap it
    res_dict = result if isinstance(result, dict) else {"result": result}

    # 1. Inventory Evidence
    if tool_name in inventory_tools:
        items = []
        if tool_name == "find_workload":
            items = res_dict.get("pods", []) + res_dict.get("deployments", []) + res_dict.get("services", [])
        elif tool_name == "get_namespaces":
            items = res_dict.get("namespaces", [])
        elif tool_name == "get_nodes":
            items = res_dict.get("nodes", [])
        elif tool_name == "list_namespace_resources":
            items = (
                res_dict.get("pods", []) +
                res_dict.get("deployments", []) +
                res_dict.get("services", []) +
                res_dict.get("statefulsets", []) +
                res_dict.get("daemonsets", [])
            )
        elif tool_name == "get_pods":
            items = res_dict.get("pods", [])
        elif tool_name == "list_services":
            items = res_dict.get("services", [])
        elif tool_name == "list_kubeconfig_contexts":
            items = res_dict.get("contexts", [])
        elif tool_name == "kb_search":
            items = res_dict.get("results", [])
        elif tool_name == "get_fix_commands":
            items = res_dict.get("commands", []) if isinstance(res_dict.get("commands"), list) else [{"commands": res_dict}]
        else:
            items = [res_dict]

        evidence = InventoryEvidence(
            items=[i if isinstance(i, dict) else {"item": i} for i in items],
            total_count=len(items),
            filter_criteria={str(k): str(v) for k, v in params.items() if v is not None}
        )
        verdict = "n/a"
        raw_excerpt = _truncate_raw(json.dumps(result), tool_name)

    # 2. Diagnostic Evidence
    elif tool_name in diagnostic_tools:
        primary_target = {str(k): v for k, v in params.items() if v is not None}
        failure_modes = []
        contributing_factors = []
        timeline = []
        verdict = "Unknown"

        if tool_name == "investigate_workload":
            primary_target.update({
                "workload_name": res_dict.get("workload_name"),
                "workload_type": res_dict.get("workload_type"),
                "namespace": res_dict.get("namespace")
            })
            pods = res_dict.get("related_pods_summary", {}).get("pods", [])
            if not pods:
                confidence.data_completeness = "stale"
            else:
                unhealthy_pods = []
                for p in pods:
                    if p.get("status") not in ("Running", "Succeeded") or p.get("ready") is False:
                        unhealthy_pods.append(p)
                verdict = "Unhealthy" if unhealthy_pods else "Healthy"
                if unhealthy_pods:
                    failure_modes.append(_with_priority({"unhealthy_pods": unhealthy_pods}, "primary_failure"))
            timeline = res_dict.get("events_parsed", {}).get("events", [])
            if res_dict.get("ai"):
                contributing_factors = [_with_priority({"ai": res_dict.get("ai")}, "ai_advisory")]
            confidence.events_analyzed = res_dict.get("events_parsed", {}).get("event_count", 0)

        elif tool_name == "analyze_namespace":
            resources = res_dict.get("resources", {})
            has_resources = any(resources.get(k) for k in ("pods", "deployments", "services", "statefulsets", "daemonsets"))
            if not has_resources:
                confidence.data_completeness = "stale"
            else:
                verdict = "Healthy"
                issue_sum = res_dict.get("issue_summary", {})
                unhealthy_pod_count = issue_sum.get("unhealthy_pod_count", 0)
                unavailable_wl_count = issue_sum.get("unavailable_workload_count", 0)
                if unhealthy_pod_count > 0 or unavailable_wl_count > 0:
                    verdict = "Unhealthy"
                    failure_modes.append(_with_priority({
                        "unhealthy_pods": issue_sum.get("unhealthy_pods", []),
                        "unavailable_workloads": issue_sum.get("unavailable_workloads", [])
                    }, "primary_failure"))
                elif issue_sum.get("warning_event_group_count", 0) > 0:
                    verdict = "Degraded"
                    failure_modes.append(_with_priority({"warning_events": issue_sum.get("warning_event_groups", [])}, "primary_failure"))
            timeline = res_dict.get("events", {}).get("events", [])
            if isinstance(res_dict.get("issue_summary"), dict):
                contributing_factors = [
                    _with_priority(item, "dependency_check")
                    for item in res_dict.get("issue_summary", {}).get("services_without_ready_endpoints", [])
                ]
            confidence.events_analyzed = len(timeline)

        elif tool_name == "investigate_node":
            node_data = res_dict.get("node", {})
            conditions = node_data.get("status", {}).get("conditions", []) if isinstance(node_data, dict) else []
            if not conditions:
                confidence.data_completeness = "stale"
            else:
                verdict = "Healthy"
                for cond in conditions:
                    if cond.get("type") == "Ready" and cond.get("status") != "True":
                        verdict = "Unhealthy"
                        failure_modes.append(_with_priority({"condition": cond}, "primary_failure"))
                    elif cond.get("type") != "Ready" and cond.get("status") == "True":
                        verdict = "Degraded"
                        failure_modes.append(_with_priority({"condition": cond}, "primary_failure"))
            timeline = res_dict.get("events", {}).get("events", [])
            confidence.events_analyzed = len(timeline)

        elif tool_name in ("analyze_error", "cluster_report", "error_summary", "generate_runbook"):
            findings = res_dict.get("findings", [])
            if tool_name == "generate_runbook":
                verdict = "n/a"
            elif findings:
                verdict = "Unhealthy"
                failure_modes.append(_with_priority({"findings": findings}, "ai_advisory"))
            else:
                verdict = "Healthy"

        evidence = DiagnosticEvidence(
            primary_target=primary_target,
            failure_modes=failure_modes,
            contributing_factors=contributing_factors,
            timeline=[t if isinstance(t, dict) else {"event": t} for t in timeline]
        )
        raw_excerpt = _truncate_raw(json.dumps(result), tool_name)

    # 3. Status Check Evidence
    elif tool_name in status_check_tools:
        target = {str(k): v for k, v in params.items() if v is not None}
        current_state = ""
        desired_state = ""
        drift = []
        verdict = "Healthy"

        if tool_name == "get_deployment":
            target.update({"deployment_name": res_dict.get("name"), "namespace": res_dict.get("namespace")})
            replicas = res_dict.get("replicas", 0)
            ready_replicas = res_dict.get("ready_replicas", 0)
            current_state = f"{ready_replicas}/{replicas} ready replicas"
            desired_state = f"{replicas} replicas"
            if ready_replicas == replicas:
                verdict = "Healthy"
            elif ready_replicas > 0:
                verdict = "Degraded"
            else:
                verdict = "Unhealthy"

        elif tool_name == "get_service":
            target.update({"service_name": res_dict.get("name"), "namespace": res_dict.get("namespace")})
            current_state = f"Type: {res_dict.get('type')}, ClusterIP: {res_dict.get('cluster_ip')}"
            desired_state = f"Selector: {res_dict.get('selector')}"

        elif tool_name == "get_endpoints":
            target.update({"service_name": res_dict.get("service_name"), "namespace": res_dict.get("namespace")})
            ready_count = res_dict.get("ready_count", 0)
            not_ready_count = res_dict.get("not_ready_count", 0)
            current_state = f"{ready_count} ready endpoints, {not_ready_count} not ready"
            desired_state = "Endpoints available"
            if ready_count > 0:
                verdict = "Healthy"
            elif not_ready_count > 0:
                verdict = "Degraded"
            else:
                verdict = "Unhealthy"

        elif tool_name == "get_rollout_status":
            current_state = res_dict.get("message", "")
            desired_state = "Rollout complete"
            if "successfully rolled out" in current_state.lower():
                verdict = "Healthy"
            elif "waiting" in current_state.lower():
                verdict = "Degraded"
            else:
                verdict = "Unknown"

        elif tool_name == "get_resource_graph":
            current_state = f"{len(res_dict.get('nodes', []))} nodes, {len(res_dict.get('edges', []))} edges"
            desired_state = "Graph populated"
            verdict = "n/a"

        elif tool_name == "switch_kubeconfig_context":
            current_state = res_dict.get("message", "")
            desired_state = f"Active context: {res_dict.get('context_name')}"
            verdict = "Healthy" if res_dict.get("success") else "Unhealthy"

        evidence = StatusCheckEvidence(
            target=target,
            current_state=current_state,
            desired_state=desired_state,
            drift=drift
        )
        raw_excerpt = _truncate_raw(res_dict.get("message") if "message" in res_dict else json.dumps(result), tool_name)

    else:
        # Fallback default: InventoryEvidence
        # TODO: log a warning when this branch fires so we notice new tools that need explicit classification.
        logger.warning(
            "Tool '%s' was wrapped in fallback InventoryEvidence. "
            "Please add it to the appropriate tool category list in make_generic_envelope.",
            tool_name
        )
        evidence = InventoryEvidence(
            items=[res_dict],
            total_count=1,
            filter_criteria={str(k): str(v) for k, v in params.items() if v is not None}
        )
        verdict = "n/a"
        raw_excerpt = _truncate_raw(json.dumps(result), tool_name)

    if not res_dict.get("success", True):
        verdict = "Unknown"
        confidence.data_completeness = "partial"

    return ToolEnvelope(
        verdict=verdict,
        evidence=evidence,
        raw_excerpt=raw_excerpt,
        confidence_signals=confidence,
        _meta=meta
    )
