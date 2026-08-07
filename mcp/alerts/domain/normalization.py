from __future__ import annotations

from datetime import datetime
from typing import Any

from alerts.domain.alert import Alert
from alerts.domain.enums import AlertSource


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_alert_payload(payload: dict[str, Any]) -> list[Alert]:
    source = detect_source(payload)
    if source == AlertSource.ALERTMANAGER:
        return _normalize_alertmanager(payload)
    if source == AlertSource.GRAFANA:
        return [_normalize_grafana(payload)]
    if source == AlertSource.LOKI:
        return [_normalize_loki(payload)]
    return [_normalize_unknown(payload)]


def detect_source(payload: dict[str, Any]) -> AlertSource:
    if isinstance(payload.get("alerts"), list):
        return AlertSource.ALERTMANAGER
    if "ruleName" in payload or payload.get("orgId") is not None:
        return AlertSource.GRAFANA
    if "streams" in payload or payload.get("source") == "loki":
        return AlertSource.LOKI
    return AlertSource.UNKNOWN


def _normalize_alertmanager(payload: dict[str, Any]) -> list[Alert]:
    alerts = []
    for item in payload.get("alerts", []):
        labels = {str(k): str(v) for k, v in item.get("labels", {}).items()}
        annotations = {str(k): str(v) for k, v in item.get("annotations", {}).items()}
        alerts.append(
            Alert.from_parts(
                source=AlertSource.ALERTMANAGER,
                name=labels.get(
                    "alertname", payload.get("groupLabels", {}).get("alertname", "unknown")
                ),
                status=item.get("status", payload.get("status", "firing")),
                severity=labels.get("severity", "unknown"),
                labels=labels,
                annotations=annotations,
                starts_at=_parse_datetime(item.get("startsAt")),
                ends_at=_parse_datetime(item.get("endsAt")),
                generator_url=item.get("generatorURL"),
                raw_payload=item,
                # Alertmanager computes its own fingerprint from the label set
                # and sends it on every delivery of the same alert — including
                # the resolved one. Prefer it: it is the upstream's own notion
                # of "this is the same alert", so dedup agrees with the system
                # that decided to re-send in the first place.
                fingerprint=item.get("fingerprint") or None,
            )
        )
    return alerts


def _normalize_grafana(payload: dict[str, Any]) -> Alert:
    labels = {str(k): str(v) for k, v in payload.get("labels", {}).items()}
    annotations = {str(k): str(v) for k, v in payload.get("annotations", {}).items()}
    name = (
        payload.get("ruleName")
        or labels.get("alertname")
        or payload.get("title")
        or "grafana_alert"
    )
    return Alert.from_parts(
        source=AlertSource.GRAFANA,
        name=str(name),
        status=str(payload.get("state", payload.get("status", "firing"))).lower(),
        severity=labels.get("severity", "unknown"),
        labels=labels,
        annotations=annotations,
        generator_url=payload.get("ruleUrl") or payload.get("dashboardURL"),
        raw_payload=payload,
    )


def _normalize_loki(payload: dict[str, Any]) -> Alert:
    labels = {str(k): str(v) for k, v in payload.get("labels", {}).items()}
    annotations = {str(k): str(v) for k, v in payload.get("annotations", {}).items()}
    name = payload.get("alertname") or labels.get("alertname") or "loki_alert"
    return Alert.from_parts(
        source=AlertSource.LOKI,
        name=str(name),
        status=str(payload.get("status", "firing")),
        severity=labels.get("severity", "unknown"),
        labels=labels,
        annotations=annotations,
        raw_payload=payload,
    )


def _normalize_unknown(payload: dict[str, Any]) -> Alert:
    labels = {str(k): str(v) for k, v in payload.get("labels", {}).items()}
    annotations = {str(k): str(v) for k, v in payload.get("annotations", {}).items()}
    return Alert.from_parts(
        source=AlertSource.UNKNOWN,
        name=str(payload.get("alertname", payload.get("name", "unknown"))),
        status=str(payload.get("status", "firing")),
        severity=labels.get("severity", str(payload.get("severity", "unknown"))),
        labels=labels,
        annotations=annotations,
        raw_payload=payload,
    )
