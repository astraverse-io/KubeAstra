from __future__ import annotations

from alerts.domain.alert import Alert


LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "namespace": (
        "namespace",
        "kubernetes_namespace",
        "namespace_name",
        "k8s_namespace",
    ),
    "pod": (
        "pod",
        "pod_name",
        "kubernetes_pod_name",
        "k8s_pod",
        "workload_pod",
    ),
    "alertname": (
        "alertname",
        "alert_name",
        "rule",
        "condition",
    ),
    "instance": (
        "instance",
        "node",
        "node_name",
        "kubernetes_node",
    ),
}


def normalize_alert_labels(alert: Alert) -> Alert:
    labels, applied_aliases = normalize_labels(alert.labels)
    if labels == alert.labels:
        return alert

    raw_payload = dict(alert.raw_payload)
    raw_payload["label_normalization"] = {
        "applied_aliases": applied_aliases,
        "original_labels": dict(alert.labels),
    }
    return alert.model_copy(update={"labels": labels, "raw_payload": raw_payload})


def normalize_labels(labels: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    normalized = dict(labels)
    applied_aliases: dict[str, str] = {}
    for canonical, aliases in LABEL_ALIASES.items():
        if normalized.get(canonical):
            continue
        for alias in aliases:
            value = labels.get(alias)
            if value:
                normalized[canonical] = value
                applied_aliases[canonical] = alias
                break
    return normalized, applied_aliases
