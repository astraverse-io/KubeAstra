from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from alerts.domain.enums import AlertSource


class Alert(BaseModel):
    source: AlertSource
    name: str
    status: str = "firing"
    severity: str = "unknown"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ends_at: datetime | None = None
    generator_url: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    fingerprint: str

    @classmethod
    def from_parts(
        cls,
        *,
        source: AlertSource,
        name: str,
        status: str = "firing",
        severity: str = "unknown",
        labels: dict[str, str] | None = None,
        annotations: dict[str, str] | None = None,
        raw_payload: dict[str, Any] | None = None,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        generator_url: str | None = None,
        fingerprint: str | None = None,
    ) -> Alert:
        labels = labels or {}
        annotations = annotations or {}
        raw_payload = raw_payload or {}
        # Annotations are deliberately NOT part of this.
        #
        # They are descriptive text, and in practice they are templated:
        # `"CPU is {{ $value }}%"` renders as "CPU is 93%", then "CPU is 94%"
        # on the next delivery. Hashing them gave the same ongoing alert a new
        # fingerprint every time it was re-sent, so dedup and resolved-matching
        # both looked wired up and quietly never fired on any Alertmanager with
        # a templated description — which is the default setup.
        #
        # What identifies an alert is what it is about: source, name, labels,
        # and when this firing episode began.
        fingerprint = fingerprint or hashlib.sha256(
            json.dumps(
                {
                    "source": source,
                    "name": name,
                    "labels": labels,
                    "starts_at": starts_at.isoformat() if starts_at else None,
                },
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            source=source,
            name=name,
            status=status,
            severity=severity,
            labels=labels,
            annotations=annotations,
            starts_at=starts_at or datetime.now(UTC),
            ends_at=ends_at,
            generator_url=generator_url,
            raw_payload=raw_payload,
            fingerprint=fingerprint,
        )


class AlertClassification(BaseModel):
    category: str
    confidence: float = Field(ge=0.0, le=1.0)
    matched_rules: list[str] = Field(default_factory=list)
    playbook_id: str | None = None
