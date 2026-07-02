from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class Finding(BaseModel):
    title: str
    detail: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list)


class RootCauseAnalysis(BaseModel):
    summary: str
    confidence: float = Field(ge=0.0, le=1.0)
    root_causes: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    supporting_evidence: list[str] = Field(default_factory=list)
    what_was_ruled_out: list[str] = Field(default_factory=list)
    next_checks: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    rca_scoring: dict[str, Any] | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
