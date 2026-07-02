from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class SemanticIncidentRecord(BaseModel):
    investigation_id: str
    alert_name: str
    category: str
    severity: str
    cluster: str | None = None
    namespace: str | None = None
    workload: str | None = None
    rca_summary: str
    root_causes: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    evidence_summaries: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def embedding_text(self) -> str:
        return "\n".join(
            [
                f"Alert: {self.alert_name}",
                f"Category: {self.category}",
                f"Severity: {self.severity}",
                f"RCA: {self.rca_summary}",
                "Root causes: " + "; ".join(self.root_causes),
                "Recommendations: " + "; ".join(self.recommendations),
                "Evidence: " + "; ".join(self.evidence_summaries),
            ]
        )
