from enum import StrEnum


class AlertSource(StrEnum):
    ALERTMANAGER = "alertmanager"
    GRAFANA = "grafana"
    LOKI = "loki"
    MANUAL = "manual"
    UNKNOWN = "unknown"


class InvestigationStatus(StrEnum):
    RECEIVED = "received"
    CLASSIFIED = "classified"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class EvidenceType(StrEnum):
    KUBERNETES = "kubernetes"
    PROMETHEUS = "prometheus"
    LOKI = "loki"
    LLM_ANALYSIS = "llm_analysis"
