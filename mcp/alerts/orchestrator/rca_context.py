from __future__ import annotations

import json
import re
from typing import Any

from alerts.domain.evidence import Evidence
from alerts.domain.investigation import Investigation

MAX_LOG_LINES = 20
MAX_EVENTS = 10
MAX_PROMETHEUS_SAMPLES = 5
WAL_REPLAY_PATTERN = r"\b(wal|tsdb|replay(?:ing)?|segment)\b"
MEMORY_EXHAUSTION_PATTERN = r"\b(killed|oom|out of memory)\b"
CPU_INTENSIVE_WORK_PATTERN = (
    r"\b(cpu\s+loop|loop\s+iterations|busy\s+loop|spinning|tight\s+loop|compute\s+loop)\b"
)
PROMETHEUS_DATA_MOUNT_PATTERN = r"(^/prometheus\b|/prometheus\b|/data\b|prometheus|thanos|tsdb|wal)"


def build_rca_context(investigation: Investigation) -> dict[str, Any]:
    evidence_details = [_evidence_detail(evidence) for evidence in investigation.evidence]
    return {
        "alert": investigation.alert.model_dump(mode="json"),
        "classification": investigation.classification.model_dump(mode="json")
        if investigation.classification
        else None,
        "selected_playbook": investigation.selected_playbook,
        "evidence_summaries": [evidence.summary for evidence in investigation.evidence],
        "evidence_details": evidence_details,
        "detected_signals": extract_evidence_signals(investigation.evidence),
    }


def extract_evidence_signals(evidence_list: list[Evidence]) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for evidence in evidence_list:
        raw = _raw_payload(evidence)
        _append_investigate_pod_signals(signals, evidence, raw)
        _append_pod_signals(signals, evidence, raw)
        _append_event_signals(signals, evidence, raw)
        _append_log_signals(signals, evidence, raw)
        _append_pvc_signals(signals, evidence, raw)
        _append_prometheus_signals(signals, evidence, raw)
    _append_cross_evidence_signals(signals, evidence_list)
    return signals


def _evidence_detail(evidence: Evidence) -> dict[str, Any]:
    raw = _raw_payload(evidence)
    detail: dict[str, Any] = {
        "evidence_id": evidence.evidence_id,
        "type": evidence.evidence_type,
        "tool": evidence.tool,
        "summary": evidence.summary,
    }
    if evidence.tool == "describe_pod":
        detail["pod_state"] = {
            "namespace": raw.get("namespace"),
            "pod": raw.get("pod") or raw.get("target"),
            "phase": raw.get("phase"),
            "node": raw.get("node"),
            "restart_count": raw.get("restart_count"),
            "waiting_reasons": raw.get("waiting_reasons", []),
            "conditions": raw.get("conditions", {}),
            "last_state": raw.get("last_state"),
            "exit_code": raw.get("exit_code"),
            "containers": raw.get("containers", []),
            "volumes": raw.get("volumes", []),
            "readonly": raw.get("readonly"),
        }
    elif evidence.tool == "get_events":
        detail["events"] = [
            {
                "type": item.get("type"),
                "reason": item.get("reason"),
                "message": item.get("message"),
                "count": item.get("count"),
                "last_timestamp": item.get("last_timestamp"),
            }
            for item in raw.get("events", [])[:MAX_EVENTS]
        ]
    elif evidence.tool == "get_logs":
        detail["logs"] = {
            "current": _tail_lines(raw.get("current_logs") or raw.get("logs") or []),
            "previous": _tail_lines(raw.get("previous_logs") or []),
            "total_log_lines": raw.get("total_log_lines"),
            "tail_lines": raw.get("tail_lines"),
        }
    elif evidence.tool == "get_pod_logs":
        detail["logs"] = {
            "namespace": raw.get("namespace"),
            "pod": raw.get("pod_name"),
            "container": raw.get("container"),
            "previous": raw.get("previous"),
            "current": [] if raw.get("previous") else _tail_lines(raw.get("logs") or []),
            "previous_lines": _tail_lines(raw.get("logs") or []) if raw.get("previous") else [],
            "error": raw.get("error"),
        }
    elif evidence.tool == "investigate_pod":
        detail["pod_investigation"] = _investigate_pod_detail(raw)
    elif evidence.tool == "describe_pod_pvcs":
        detail["pvc_state"] = {
            "namespace": raw.get("namespace"),
            "pod": raw.get("pod"),
            "pvcs": raw.get("pvcs", []),
            "readonly": raw.get("readonly"),
        }
    elif evidence.tool == "prom_query":
        result = raw.get("result", [])
        detail["prometheus"] = {
            "query": raw.get("query"),
            "result_type": raw.get("result_type"),
            "result_count": len(result) if isinstance(result, list) else None,
            "sample": result[:MAX_PROMETHEUS_SAMPLES] if isinstance(result, list) else [],
            "error": raw.get("error"),
        }
    else:
        detail["raw"] = raw
    return detail


def _raw_payload(evidence: Evidence) -> dict[str, Any]:
    """Return structured tool output, including legacy rows stored as raw.output."""
    raw = evidence.raw or {}
    output = raw.get("output")
    if len(raw) == 1 and isinstance(output, dict):
        return output
    if len(raw) == 1 and isinstance(output, str):
        try:
            decoded = json.loads(output)
        except json.JSONDecodeError:
            return raw
        if isinstance(decoded, dict):
            return decoded
    return raw


def _investigate_pod_detail(raw: dict[str, Any]) -> dict[str, Any]:
    classification = raw.get("classification") or {}
    describe = raw.get("describe") or {}
    highlights = describe.get("highlights") or {}
    events = (raw.get("events") or {}).get("events") or []
    logs_current = raw.get("logs_current") or {}
    logs_previous = raw.get("logs_previous") or {}
    ai = raw.get("ai") or {}
    ai_analysis = ai.get("ai_analysis") if isinstance(ai, dict) else None
    evidence_summary = raw.get("evidence_summary") or {}

    return {
        "namespace": raw.get("namespace"),
        "pod": raw.get("pod_name"),
        "classification": classification,
        "playbook": raw.get("playbook"),
        "steps_run": raw.get("steps_run", []),
        "pod_state": {
            "phase": (raw.get("pod_spec_summary") or {}).get("phase"),
            "restart_count": highlights.get("restart_count"),
            "state": highlights.get("state"),
            "last_state": highlights.get("last_state"),
            "ready": highlights.get("ready"),
            "conditions": highlights.get("conditions", []),
            "warnings": highlights.get("warnings", []),
        },
        "logs": {
            "current": _tail_lines(logs_current.get("logs") or logs_current.get("logs_summary") or []),
            "previous": _tail_lines(logs_previous.get("logs") or logs_previous.get("logs_summary") or []),
            "current_error": logs_current.get("error"),
            "previous_error": logs_previous.get("error"),
        },
        "container_log_findings": _compact_container_log_findings(
            raw.get("container_log_findings") or []
        ),
        "deterministic_evidence": evidence_summary,
        "ai_analysis": ai_analysis,
        "events": [
            {
                "type": item.get("type"),
                "reason": item.get("reason"),
                "message": item.get("message"),
                "count": item.get("count"),
                "last_timestamp": item.get("last_timestamp"),
            }
            for item in events[:MAX_EVENTS]
        ],
    }


def _compact_container_log_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for finding in findings[:8]:
        compact.append(
            {
                "container": finding.get("container"),
                "ready": finding.get("ready"),
                "restart_count": finding.get("restart_count"),
                "state": finding.get("state"),
                "reason": finding.get("reason"),
                "message": finding.get("message"),
                "last_exit_code": finding.get("last_exit_code"),
                "last_reason": finding.get("last_reason"),
                "logs_current": finding.get("logs_current"),
                "logs_previous": finding.get("logs_previous"),
                "diagnostic_issue": finding.get("diagnostic_issue"),
            }
        )
    return compact


def _append_pod_signals(
    signals: list[dict[str, Any]], evidence: Evidence, raw: dict[str, Any]
) -> None:
    restart_count = _as_int(raw.get("restart_count"))
    if restart_count and restart_count > 0:
        signals.append(
            {
                "name": "pod_restarting",
                "severity": "warning" if restart_count < 10 else "critical",
                "evidence_id": evidence.evidence_id,
                "detail": f"Pod restart count is {restart_count}.",
            }
        )
    if restart_count and restart_count >= 100:
        signals.append(
            {
                "name": "severe_restart_loop",
                "severity": "critical",
                "evidence_id": evidence.evidence_id,
                "detail": f"Pod has a very high restart count: {restart_count}.",
            }
        )
    for reason in raw.get("waiting_reasons", []):
        signals.append(
            {
                "name": "container_waiting_reason",
                "severity": "warning",
                "evidence_id": evidence.evidence_id,
                "detail": f"Container waiting reason is {reason}.",
            }
        )


def _append_investigate_pod_signals(
    signals: list[dict[str, Any]], evidence: Evidence, raw: dict[str, Any]
) -> None:
    if evidence.tool != "investigate_pod":
        return

    classification = raw.get("classification") or {}
    mode = classification.get("mode")
    container = classification.get("container")
    reason = classification.get("reason")
    if mode and mode != "other":
        signals.append(
            {
                "name": "pod_failure_mode",
                "severity": "critical" if mode in {"CrashLoopBackOff", "OOMKilled"} else "warning",
                "evidence_id": evidence.evidence_id,
                "detail": (
                    f"investigate_pod classified {raw.get('namespace')}/{raw.get('pod_name')} "
                    f"as {mode}"
                    + (f" in container {container}" if container else "")
                    + (f" ({reason})" if reason else "")
                    + "."
                ),
                "mode": mode,
                "container": container,
                "reason": reason,
            }
        )

    for finding in raw.get("container_log_findings") or []:
        restart_count = _as_int(finding.get("restart_count"))
        if restart_count and restart_count > 0:
            signals.append(
                {
                    "name": "pod_restarting",
                    "severity": "warning" if restart_count < 10 else "critical",
                    "evidence_id": evidence.evidence_id,
                    "detail": (
                        f"Container {finding.get('container')} restart count is "
                        f"{restart_count}."
                    ),
                }
            )
        diagnostic = finding.get("diagnostic_issue")
        if isinstance(diagnostic, dict):
            signals.append(
                {
                    "name": diagnostic.get("type", "container_log_diagnostic"),
                    "severity": "critical",
                    "evidence_id": evidence.evidence_id,
                    "detail": (
                        f"Container {finding.get('container')} logs contain "
                        "a deterministic diagnostic issue."
                    ),
                    "diagnostic": diagnostic,
                }
            )

    evidence_summary = raw.get("evidence_summary") or {}
    suspected_root_cause = evidence_summary.get("suspected_root_cause")
    if suspected_root_cause:
        signals.append(
            {
                "name": "deterministic_root_cause",
                "severity": "critical",
                "evidence_id": evidence.evidence_id,
                "detail": suspected_root_cause,
                "suggested_fix": evidence_summary.get("suggested_fix"),
            }
        )


def _append_event_signals(
    signals: list[dict[str, Any]], evidence: Evidence, raw: dict[str, Any]
) -> None:
    events = raw.get("events", [])
    if isinstance(events, dict):
        events = events.get("events", [])
    for event in events:
        if not isinstance(event, dict):
            continue
        reason = event.get("reason")
        message = event.get("message")
        if reason:
            signals.append(
                {
                    "name": "kubernetes_event_reason",
                    "severity": "warning" if event.get("type") == "Warning" else "info",
                    "evidence_id": evidence.evidence_id,
                    "detail": f"Event reason {reason}: {message}",
                }
            )


def _append_log_signals(
    signals: list[dict[str, Any]], evidence: Evidence, raw: dict[str, Any]
) -> None:
    if evidence.tool == "investigate_pod":
        logs_current = raw.get("logs_current") or {}
        logs_previous = raw.get("logs_previous") or {}
        current_logs = logs_current.get("logs") or logs_current.get("logs_summary") or []
        previous_logs = logs_previous.get("logs") or logs_previous.get("logs_summary") or []
    else:
        current_logs = raw.get("current_logs") or raw.get("logs") or []
        previous_logs = raw.get("previous_logs") or []
    if evidence.tool == "get_pod_logs" and raw.get("previous"):
        previous_logs = raw.get("logs") or []
        current_logs = []
    current_logs = _as_lines(current_logs)
    previous_logs = _as_lines(previous_logs)
    all_lines = [str(line) for line in [*current_logs, *previous_logs]]
    if raw.get("total_log_lines") == 0 or (not current_logs and not previous_logs):
        signals.append(
            {
                "name": "logs_empty",
                "severity": "warning",
                "evidence_id": evidence.evidence_id,
                "detail": "Current and previous log excerpts are empty.",
            }
        )
        return

    joined = "\n".join(all_lines)
    if re.search(MEMORY_EXHAUSTION_PATTERN, joined, flags=re.IGNORECASE):
        signals.append(
            {
                "name": "process_killed_or_memory_exhaustion",
                "severity": "critical",
                "evidence_id": evidence.evidence_id,
                "detail": "Logs contain killed/OOM/out-of-memory indicators.",
                "supporting_lines": _matching_lines(all_lines, MEMORY_EXHAUSTION_PATTERN),
            }
        )
    if re.search(WAL_REPLAY_PATTERN, joined, flags=re.IGNORECASE):
        has_memory_exhaustion = re.search(
            MEMORY_EXHAUSTION_PATTERN,
            joined,
            flags=re.IGNORECASE,
        )
        signals.append(
            {
                "name": "prometheus_wal_replay_suspected",
                "severity": "critical" if has_memory_exhaustion else "warning",
                "evidence_id": evidence.evidence_id,
                "detail": "Logs mention WAL/TSDB replay or segment processing during startup.",
                "supporting_lines": _matching_lines(all_lines, WAL_REPLAY_PATTERN),
            }
        )
    if re.search(CPU_INTENSIVE_WORK_PATTERN, joined, flags=re.IGNORECASE):
        signals.append(
            {
                "name": "application_cpu_intensive_work",
                "severity": "warning",
                "evidence_id": evidence.evidence_id,
                "detail": "Logs indicate repeated CPU-intensive loop or compute work.",
                "confidence": 0.8,
                "supporting_lines": _matching_lines(all_lines, CPU_INTENSIVE_WORK_PATTERN),
            }
        )
    allocation_matches = [
        int(match.group(1))
        for line in all_lines
        if (match := re.search(r"Allocated\s+(\d+)\s*MB", line, flags=re.IGNORECASE))
    ]
    if allocation_matches:
        peak = max(allocation_matches)
        signals.append(
            {
                "name": "memory_growth_pattern",
                "severity": "warning",
                "evidence_id": evidence.evidence_id,
                "detail": f"Logs show memory allocation increasing up to {peak}MB.",
                "peak_observed_mb": peak,
            }
        )


def _append_pvc_signals(
    signals: list[dict[str, Any]], evidence: Evidence, raw: dict[str, Any]
) -> None:
    pvcs = raw.get("pvcs") or []
    if not pvcs:
        return
    signals.append(
        {
            "name": "pod_uses_persistent_storage",
            "severity": "info",
            "evidence_id": evidence.evidence_id,
            "detail": f"Pod has {len(pvcs)} mounted PVC(s).",
            "pvc_names": [pvc.get("name") for pvc in pvcs if pvc.get("name")],
        }
    )
    for pvc in pvcs:
        for mount in pvc.get("mounts", []):
            mount_text = " ".join(
                str(value)
                for value in [
                    pvc.get("name"),
                    mount.get("container"),
                    mount.get("mount_path"),
                    mount.get("volume_name"),
                ]
                if value
            )
            if re.search(PROMETHEUS_DATA_MOUNT_PATTERN, mount_text, flags=re.IGNORECASE):
                signals.append(
                    {
                        "name": "prometheus_data_pvc_mounted",
                        "severity": "info",
                        "evidence_id": evidence.evidence_id,
                        "detail": (
                            f"PVC {pvc.get('name')} is mounted at "
                            f"{mount.get('mount_path')} for container {mount.get('container')}."
                        ),
                        "pvc_name": pvc.get("name"),
                        "mount_path": mount.get("mount_path"),
                    }
                )
        if pvc.get("phase") and pvc.get("phase") != "Bound":
            signals.append(
                {
                    "name": "pvc_not_bound",
                    "severity": "warning",
                    "evidence_id": evidence.evidence_id,
                    "detail": f"PVC {pvc.get('name')} is in phase {pvc.get('phase')}.",
                }
            )


def _append_cross_evidence_signals(
    signals: list[dict[str, Any]], evidence_list: list[Evidence]
) -> None:
    has_wal_replay = any(
        signal.get("name") == "prometheus_wal_replay_suspected" for signal in signals
    )
    has_memory_exhaustion = any(
        signal.get("name") == "process_killed_or_memory_exhaustion" for signal in signals
    )
    has_persistent_storage = any(
        signal.get("name") in {"pod_uses_persistent_storage", "prometheus_data_pvc_mounted"}
        for signal in signals
    )
    has_restart_loop = any(
        signal.get("name") in {"pod_restarting", "severe_restart_loop", "container_waiting_reason"}
        for signal in signals
    )
    if not (has_wal_replay and has_memory_exhaustion and has_persistent_storage):
        return

    evidence_ids = [evidence.evidence_id for evidence in evidence_list]
    severity = "critical" if has_restart_loop else "warning"
    signals.append(
        {
            "name": "wal_replay_from_persistent_storage_suspected",
            "severity": severity,
            "evidence_id": evidence_ids[-1] if evidence_ids else None,
            "evidence_ids": evidence_ids,
            "detail": (
                "Prometheus may be crash-looping because WAL/TSDB replay from persisted "
                "PVC data is coinciding with memory exhaustion."
            ),
        }
    )


def _append_prometheus_signals(
    signals: list[dict[str, Any]], evidence: Evidence, raw: dict[str, Any]
) -> None:
    if raw.get("error"):
        signals.append(
            {
                "name": "prometheus_query_error",
                "severity": "warning",
                "evidence_id": evidence.evidence_id,
                "detail": str(raw["error"]),
            }
        )
    result = raw.get("result")
    if isinstance(result, list) and len(result) == 0:
        signals.append(
            {
                "name": "prometheus_empty_result",
                "severity": "info",
                "evidence_id": evidence.evidence_id,
                "detail": f"Prometheus query returned no series: {raw.get('query')}",
            }
        )


def _tail_lines(lines: Any) -> list[str]:
    lines = _as_lines(lines)
    return [str(line) for line in lines[-MAX_LOG_LINES:]]


def _as_lines(value: Any) -> list[Any]:
    if isinstance(value, str):
        return value.splitlines()
    if isinstance(value, list):
        return value
    return [value] if value else []


def _matching_lines(lines: list[str], pattern: str) -> list[str]:
    return [line for line in lines if re.search(pattern, line, flags=re.IGNORECASE)][
        -MAX_LOG_LINES:
    ]


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
