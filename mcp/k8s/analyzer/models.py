"""Data models for Kubernetes health analyzer findings and evidence references."""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


@dataclass
class EvidenceRef:
    """Reference to evidence item collected from Kubernetes."""
    source: str          # pod_status | event | log | node_condition | service | pvc | rollout
    namespace: Optional[str] = None
    kind: Optional[str] = None
    name: Optional[str] = None
    field: Optional[str] = None
    value: Optional[str] = None
    message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Finding:
    """Structured rule finding describing a Kubernetes issue."""
    rule_id: str
    severity: str        # critical | warning | info
    category: str        # pod | node | scheduling | service | storage | rollout | resource
    title: str
    summary: str
    namespace: Optional[str] = None
    resource_kind: Optional[str] = None
    resource_name: Optional[str] = None
    evidence: List[EvidenceRef] = field(default_factory=list)
    probable_cause: Optional[str] = None
    recommended_next_steps: List[str] = field(default_factory=list)
    suggested_read_only_commands: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "summary": self.summary,
            "namespace": self.namespace,
            "resource_kind": self.resource_kind,
            "resource_name": self.resource_name,
            "evidence": [e.to_dict() for e in self.evidence],
            "probable_cause": self.probable_cause,
            "recommended_next_steps": self.recommended_next_steps,
            "suggested_read_only_commands": self.suggested_read_only_commands,
        }


@dataclass
class AnalyzerResult:
    """Top-level health analyzer result."""
    scope: Dict[str, Any]
    finding_count: int
    findings: List[Finding]
    summary: Dict[str, int]  # {"critical": x, "warning": y, "info": z}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "finding_count": self.finding_count,
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
        }
