from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from alerts.domain.actions import ToolExecutionRecord
from alerts.domain.alert import Alert, AlertClassification
from alerts.domain.enums import InvestigationStatus
from alerts.domain.evidence import Evidence
from alerts.domain.rca import Finding, RootCauseAnalysis


class AuditEvent(BaseModel):
    sequence: int
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LlmInteraction(BaseModel):
    request_id: str = Field(default_factory=lambda: f"llm_{uuid4().hex}")
    prompt_version: str
    request: dict[str, Any] = Field(default_factory=dict)
    response: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Investigation(BaseModel):
    investigation_id: str = Field(default_factory=lambda: f"inv_{uuid4().hex}")
    status: InvestigationStatus = InvestigationStatus.RECEIVED
    alert: Alert
    classification: AlertClassification | None = None
    selected_playbook: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    executed_tools: list[ToolExecutionRecord] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    llm_interactions: list[LlmInteraction] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    rca: RootCauseAnalysis | None = None
    audit_log: list[AuditEvent] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def append_audit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        self.audit_log.append(
            AuditEvent(
                sequence=len(self.audit_log) + 1,
                event_type=event_type,
                payload=payload or {},
            )
        )
        self.updated_at = datetime.now(UTC)
