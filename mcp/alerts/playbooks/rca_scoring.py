from __future__ import annotations

from typing import Any

from alerts.domain.alert import Alert, AlertClassification
from alerts.domain.evidence import Evidence
from alerts.domain.playbook import Playbook
from alerts.playbooks.decision_engine import _build_fact_index, _condition_group_matches


def evaluate_rca_scoring(
    *,
    playbook: Playbook,
    alert: Alert,
    classification: AlertClassification | None,
    evidence: list[Evidence],
    detected_signals: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Score RCA candidates from playbook config.

    Returns None when no scoring config exists or no configured candidate earns points.
    RCA generation should continue without scoring in those cases.
    """
    candidates = playbook.rca_scoring.get("candidates", [])
    if not candidates:
        return None

    facts = _build_fact_index(alert=alert, classification=classification, evidence=evidence)
    _add_derived_cpu_facts(facts, evidence)
    _add_signal_facts(facts, detected_signals)

    scored_candidates = [
        _score_candidate(candidate, facts, alert)
        for candidate in candidates
        if _candidate_applies(candidate, alert)
    ]
    scored_candidates = [candidate for candidate in scored_candidates if candidate["score"] > 0]
    if not scored_candidates:
        return None

    scored_candidates.sort(key=lambda candidate: candidate["score"], reverse=True)
    winner = scored_candidates[0]
    return {
        "candidate": winner["id"],
        "display_name": winner.get("display_name") or winner["id"],
        "score": winner["score"],
        "confidence": winner["score"] / float(winner["max_score"] or 100),
        "confidence_label": _confidence_label(
            winner["score"],
            winner.get("confidence_thresholds", {}),
        ),
        "matched_evidence": winner["matched_evidence"],
        "candidate_scores": scored_candidates,
    }


def _score_candidate(
    candidate: dict[str, Any],
    facts: dict[str, list[Any]],
    alert: Alert,
) -> dict[str, Any]:
    score = 0
    matched_evidence = []

    for rule in candidate.get("evidence", []):
        if _condition_group_matches(rule.get("when", {}), facts):
            points = int(rule.get("points", 0))
            score += points
            matched_evidence.append(_project_score_rule(rule, points))

    for rule in candidate.get("contradictions", []):
        if _condition_group_matches(rule.get("when", {}), facts):
            points = int(rule.get("points", 0))
            score += points
            matched_evidence.append(_project_score_rule(rule, points))

    max_score = int(candidate.get("max_score", 100) or 100)
    score = max(0, min(max_score, score))
    return {
        "id": candidate.get("id"),
        "display_name": candidate.get("display_name"),
        "description": candidate.get("description"),
        "alert_name": alert.name,
        "score": score,
        "max_score": max_score,
        "confidence_thresholds": candidate.get("confidence_thresholds", {}),
        "matched_evidence": matched_evidence,
    }


def _project_score_rule(rule: dict[str, Any], points: int) -> dict[str, Any]:
    return {
        "id": rule.get("id"),
        "points": points,
        "description": rule.get("description"),
    }


def _candidate_applies(candidate: dict[str, Any], alert: Alert) -> bool:
    alert_names = set(candidate.get("applies_to_alerts") or [])
    if not alert_names:
        return True
    return bool({alert.name, alert.labels.get("alertname", "")}.intersection(alert_names))


def _confidence_label(score: int, thresholds: dict[str, Any]) -> str:
    high = int(thresholds.get("high", 75))
    medium = int(thresholds.get("medium", 50))
    if score >= high:
        return "high"
    if score >= medium:
        return "medium"
    return "low"


def _add_signal_facts(
    facts: dict[str, list[Any]],
    detected_signals: list[dict[str, Any]],
) -> None:
    for signal in detected_signals:
        raw_name = str(signal.get("name") or "")
        if not raw_name:
            continue
        for path in _signal_paths(raw_name):
            _add(facts, f"{path}.present", True)
            _add(facts, f"{path}.severity", signal.get("severity"))
            _add(facts, f"{path}.confidence", signal.get("confidence", 1.0))
            _add(facts, f"{path}.evidence_id", signal.get("evidence_id"))


def _add_derived_cpu_facts(
    facts: dict[str, list[Any]],
    evidence: list[Evidence],
) -> None:
    cpu_limits = [
        limit
        for item in evidence
        if item.tool == "describe_pod"
        for limit in _pod_cpu_limits(item.raw)
    ]
    cpu_usage_samples = [
        sample
        for item in evidence
        if item.tool == "prom_query"
        for sample in _prometheus_numeric_samples(item.raw)
        if _looks_like_cpu_usage_query(item.raw)
    ]

    for limit in cpu_limits:
        _add(facts, "evidence.kubernetes.pod.cpu_limit_cores", limit)
    for sample in cpu_usage_samples:
        _add(facts, "evidence.prometheus.pod_cpu_usage_cores", sample)
        for limit in cpu_limits:
            if limit > 0:
                _add(facts, "evidence.prometheus.pod_cpu_usage_percent", (sample / limit) * 100)
                if sample >= limit * 0.95:
                    _add(facts, "signals.cpu.usage_matches_limit", True)
                    _add(facts, "signals.cpu.usage_high", True)


def _pod_cpu_limits(raw: dict[str, Any]) -> list[float]:
    limits = []
    direct_limit = _parse_cpu_cores(raw.get("cpu_limit"))
    if direct_limit is not None:
        limits.append(direct_limit)

    for container in raw.get("containers", []):
        resources = container.get("resources") or {}
        container_limits = resources.get("limits") or {}
        parsed = _parse_cpu_cores(container_limits.get("cpu"))
        if parsed is not None:
            limits.append(parsed)
    return limits


def _prometheus_numeric_samples(raw: dict[str, Any]) -> list[float]:
    samples = []
    for item in raw.get("result", []):
        value = item.get("value") if isinstance(item, dict) else None
        if isinstance(value, list) and len(value) >= 2:
            parsed = _as_float(value[1])
            if parsed is not None:
                samples.append(parsed)
        parsed_item = _as_float(item.get("value")) if isinstance(item, dict) else None
        if parsed_item is not None:
            samples.append(parsed_item)
    return samples


def _looks_like_cpu_usage_query(raw: dict[str, Any]) -> bool:
    query = str(raw.get("query") or "").lower()
    return "container_cpu_usage_seconds_total" in query or "cpu_usage" in query


def _parse_cpu_cores(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("m"):
        millicores = _as_float(text[:-1])
        return millicores / 1000 if millicores is not None else None
    return _as_float(text)


def _signal_paths(raw_name: str) -> list[str]:
    raw_path = f"signals.{raw_name}"
    parts = raw_name.split("_", 1)
    if len(parts) == 1:
        return [raw_path]
    semantic_path = f"signals.{parts[0]}.{parts[1]}"
    return [raw_path, semantic_path]


def _add(facts: dict[str, list[Any]], path: str, value: Any) -> None:
    if value is not None:
        facts.setdefault(path, []).append(value)


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
