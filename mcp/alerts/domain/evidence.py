from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from alerts.domain.enums import EvidenceType


class Evidence(BaseModel):
    evidence_id: str = Field(default_factory=lambda: f"ev_{uuid4().hex}")
    evidence_type: EvidenceType
    tool: str
    summary: str
    raw: dict[str, Any] = Field(default_factory=dict)
    collected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    references: list[str] = Field(default_factory=list)
