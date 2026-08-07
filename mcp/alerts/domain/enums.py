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
    # The alert names a cluster this deployment has no route to, so nothing was
    # investigated. Terminal: nothing advances it, and a refire after the
    # cluster is registered starts a fresh investigation, so routing repairs
    # itself without anyone reprocessing a backlog.
    NEEDS_CONFIG = "needs_config"


class EvidenceType(StrEnum):
    KUBERNETES = "kubernetes"
    PROMETHEUS = "prometheus"
    LOKI = "loki"
    LLM_ANALYSIS = "llm_analysis"
