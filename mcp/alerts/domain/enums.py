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
    # The alert stopped firing. Terminal, and distinct from COMPLETED: that
    # means the investigation finished, this means the underlying problem went
    # away — which is the only status that can tell you a time to recovery.
    RESOLVED = "resolved"


class EvidenceType(StrEnum):
    KUBERNETES = "kubernetes"
    PROMETHEUS = "prometheus"
    LOKI = "loki"
    LLM_ANALYSIS = "llm_analysis"
