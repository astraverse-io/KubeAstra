from __future__ import annotations

import ast
import json
import operator
import re
from typing import Any

from alerts.domain.alert import Alert, AlertClassification
from alerts.domain.evidence import Evidence
from alerts.domain.playbook import Playbook

COMPARATORS = {
    "==": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
}


def evaluate_decision_rules(
    *,
    playbook: Playbook,
    alert: Alert,
    classification: AlertClassification | None,
    evidence: list[Evidence],
) -> dict[str, Any]:
    facts = _build_fact_index(alert=alert, classification=classification, evidence=evidence)
    matched_rules = [
        _project_rule(rule)
        for rule in playbook.decision_rules
        if _rule_applies(rule, alert, classification)
        and _condition_group_matches(rule.get("when", {}), facts)
    ]
    matched_rules.sort(key=lambda rule: rule["priority"], reverse=True)
    return {
        "matched_rules": matched_rules,
        "preferred_steps": _ordered_unique(
            step
            for rule in matched_rules
            for step in rule.get("prefer_steps", [])
        ),
        "preferred_tools": _ordered_unique(
            tool
            for rule in matched_rules
            for tool in rule.get("prefer_tools", [])
            if tool in playbook.allowed_tools
        ),
        "focus": _ordered_unique(
            focus for rule in matched_rules for focus in rule.get("focus", [])
        ),
        "interpretations": [
            rule["interpretation"] for rule in matched_rules if rule.get("interpretation")
        ],
    }


def rank_steps_by_decision_guidance(
    remaining_steps: list[dict[str, Any]],
    decision_guidance: dict[str, Any],
) -> list[dict[str, Any]]:
    preferred_steps = decision_guidance.get("preferred_steps", [])
    preferred_tools = decision_guidance.get("preferred_tools", [])
    if not preferred_steps and not preferred_tools:
        return remaining_steps
    step_priority = {step_id: index for index, step_id in enumerate(preferred_steps)}
    tool_offset = len(step_priority)
    priority = {tool: index for index, tool in enumerate(preferred_tools)}
    return sorted(
        remaining_steps,
        key=lambda step: (
            step_priority.get(
                step["id"],
                tool_offset + priority.get(step["tool"], len(priority)),
            ),
            remaining_steps.index(step),
        ),
    )


def _project_rule(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": rule.get("id"),
        "priority": int(rule.get("priority", 0)),
        "prefer_steps": list(rule.get("prefer_steps", [])),
        "prefer_tools": list(rule.get("prefer_tools", [])),
        "focus": list(rule.get("focus", [])),
        "interpretation": rule.get("interpretation"),
    }


def _rule_applies(
    rule: dict[str, Any],
    alert: Alert,
    classification: AlertClassification | None,
) -> bool:
    applies_to = rule.get("applies_to") or {}
    categories = set(applies_to.get("alert_categories") or [])
    names = set(applies_to.get("alert_names") or [])
    reasons = set(applies_to.get("reasons") or [])
    if categories and (not classification or classification.category not in categories):
        return False
    alert_names = {alert.name, alert.labels.get("alertname", "")}
    if names and not names.intersection(alert_names):
        return False
    alert_reasons = {alert.labels.get("reason", ""), alert.annotations.get("reason", "")}
    return not reasons or bool(reasons.intersection(alert_reasons))


def _condition_group_matches(group: Any, facts: dict[str, list[Any]]) -> bool:
    if not group:
        return True
    if isinstance(group, str):
        return _condition_matches(group, facts)
    if isinstance(group, list):
        return all(_condition_group_matches(item, facts) for item in group)
    if not isinstance(group, dict):
        return False
    all_conditions = group.get("all", [])
    any_conditions = group.get("any", [])
    if all_conditions and not all(
        _condition_group_matches(condition, facts) for condition in all_conditions
    ):
        return False
    if any_conditions and not any(
        _condition_group_matches(condition, facts) for condition in any_conditions
    ):
        return False
    return bool(all_conditions or any_conditions)


def _condition_matches(condition: str, facts: dict[str, list[Any]]) -> bool:
    condition = condition.strip()
    if condition.endswith(" exists"):
        path = condition[: -len(" exists")].strip()
        return any(value not in {None, ""} for value in facts.get(path, []))
    contains = re.match(r"(.+?)\s+contains\s+(.+)", condition)
    if contains:
        path, expected = contains.groups()
        needle = str(_parse_literal(expected)).lower()
        return any(needle in str(value).lower() for value in facts.get(path.strip(), []))
    in_match = re.match(r"(.+?)\s+in\s+(.+)", condition)
    if in_match:
        path, expected = in_match.groups()
        expected_values = _as_list(_parse_literal(expected))
        return any(value in expected_values for value in facts.get(path.strip(), []))
    for symbol, comparator in COMPARATORS.items():
        if symbol in condition:
            left, right = condition.split(symbol, 1)
            expected = _parse_literal(right)
            values = facts.get(left.strip(), [])
            return any(_compare(value, expected, comparator) for value in values)
    return False


def _compare(value: Any, expected: Any, comparator: Any) -> bool:
    if isinstance(value, list):
        return any(_compare(item, expected, comparator) for item in value)
    if isinstance(expected, bool):
        return comparator(_as_bool(value), expected)
    if isinstance(expected, int | float):
        number = _as_float(value)
        return number is not None and comparator(number, float(expected))
    return comparator(str(value), str(expected))


def _parse_literal(value: str) -> Any:
    stripped = value.strip()
    lowered = stripped.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return ast.literal_eval(stripped)
    except (SyntaxError, ValueError):
        return stripped.strip("\"'")


def _build_fact_index(
    *,
    alert: Alert,
    classification: AlertClassification | None,
    evidence: list[Evidence],
) -> dict[str, list[Any]]:
    facts: dict[str, list[Any]] = {}
    _add(facts, "alert.name", alert.name)
    _add(facts, "classification.category", classification.category if classification else None)
    for key, value in alert.labels.items():
        _add(facts, f"alert.labels.{key}", value)
    for key, value in alert.annotations.items():
        _add(facts, f"alert.annotations.{key}", value)
        _add(facts, f"evidence.annotations.{key}", value)
    for item in evidence:
        raw = _raw_payload(item)
        if item.tool == "describe_pod":
            _add(facts, "evidence.kubernetes.pod.phase", raw.get("phase"))
            _add(facts, "evidence.kubernetes.pod.restart_count", raw.get("restart_count"))
            _add(facts, "evidence.kubernetes.pod.ready", _is_ready(raw.get("conditions", {})))
            last_state = raw.get("last_state") or {}
            _add(facts, "evidence.kubernetes.pod.last_state.reason", last_state.get("reason"))
            _add(facts, "evidence.kubernetes.pod.last_state.exit_code", last_state.get("exit_code"))
            for reason in raw.get("waiting_reasons", []):
                _add(facts, "evidence.kubernetes.pod.reason", reason)
        if item.tool == "get_logs":
            current_logs = raw.get("current_logs") or raw.get("logs") or []
            previous_logs = raw.get("previous_logs") or []
            _add(facts, "evidence.logs.current_empty", not current_logs)
            _add(facts, "evidence.logs.previous_empty", not previous_logs)
            for line in current_logs:
                _add(facts, "evidence.logs.current", line)
            for line in previous_logs:
                _add(facts, "evidence.logs.previous", line)
        if item.tool == "get_pod_logs":
            logs = _as_lines(raw.get("logs") or [])
            if raw.get("previous"):
                _add(facts, "evidence.logs.previous_empty", not logs)
                for line in logs:
                    _add(facts, "evidence.logs.previous", line)
            else:
                _add(facts, "evidence.logs.current_empty", not logs)
                for line in logs:
                    _add(facts, "evidence.logs.current", line)
        if item.tool == "investigate_pod":
            classification = raw.get("classification") or {}
            _add(facts, "evidence.kubernetes.pod.reason", classification.get("reason"))
            _add(facts, "evidence.kubernetes.pod.mode", classification.get("mode"))
            _add(facts, "evidence.kubernetes.pod.container", classification.get("container"))
            highlights = (raw.get("describe") or {}).get("highlights") or {}
            _add(facts, "evidence.kubernetes.pod.restart_count", highlights.get("restart_count"))
            _add(facts, "evidence.kubernetes.pod.ready", _as_bool(highlights.get("ready")))
            for finding in raw.get("container_log_findings") or []:
                _add(facts, "evidence.kubernetes.pod.reason", finding.get("reason"))
                _add(facts, "evidence.kubernetes.pod.restart_count", finding.get("restart_count"))
                for line in _as_lines((finding.get("logs_current") or {}).get("excerpt") or ""):
                    _add(facts, "evidence.logs.current", line)
                for line in _as_lines((finding.get("logs_previous") or {}).get("excerpt") or ""):
                    _add(facts, "evidence.logs.previous", line)
            for event in (raw.get("events") or {}).get("events", []):
                _add(facts, "evidence.events.reason", event.get("reason"))
                _add(facts, "evidence.events.message", event.get("message"))
        if item.tool == "get_events":
            for event in raw.get("events", []):
                _add(facts, "evidence.events.reason", event.get("reason"))
                _add(facts, "evidence.events.message", event.get("message"))
        if item.tool == "describe_pod_pvcs":
            pvcs = raw.get("pvcs") or []
            _add(facts, "evidence.kubernetes.pvc.count", len(pvcs))
            for pvc in pvcs:
                _add(facts, "evidence.kubernetes.pvc.name", pvc.get("name"))
                _add(facts, "evidence.kubernetes.pvc.phase", pvc.get("phase"))
                _add(facts, "evidence.kubernetes.pvc.storage_class", pvc.get("storage_class"))
                for mount in pvc.get("mounts", []):
                    _add(facts, "evidence.kubernetes.pvc.mount_path", mount.get("mount_path"))
        if item.tool == "prom_query":
            result = raw.get("result", [])
            _add(
                facts,
                "evidence.prometheus.result_count",
                len(result) if isinstance(result, list) else None,
            )
            for key, value in raw.items():
                if isinstance(value, int | float | str | bool):
                    _add(facts, f"evidence.prometheus.{key}", value)
    return facts


def _raw_payload(evidence: Evidence) -> dict[str, Any]:
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


def _is_ready(conditions: dict[str, Any]) -> bool | None:
    value = conditions.get("Ready")
    if value is None:
        return None
    return _as_bool(value)


def _add(facts: dict[str, list[Any]], path: str, value: Any) -> None:
    if value is not None:
        facts.setdefault(path, []).append(value)


def _ordered_unique(values: Any) -> list[Any]:
    seen = set()
    unique = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "yes", "1"}
    return bool(value)


def _as_lines(value: Any) -> list[Any]:
    if isinstance(value, str):
        return value.splitlines()
    if isinstance(value, list):
        return value
    return [value] if value else []


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
