"""Typed results for the GitOps Reconciliation Pilot.

Pure dataclasses, stdlib only — no third-party, no ``ui/``, no LLM, no DB.
``to_dict`` gives a JSON-serializable shape the server/CLI can return.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, Optional

# Deterministic root-cause buckets for a degraded/out-of-sync GitOps app.
RootCause = Literal[
    "healthy",
    "out_of_sync_drift",
    "image_pull",
    "failed_hook",
    "crd_schema_mismatch",
    "rbac",
    "progressing",
    "unknown",
]
FixRoute = Literal["gitops_pr", "remediation", "advisory", "none"]


@dataclass
class Evidence:
    source: str  # "condition" | "operationState" | "resource"
    detail: str


@dataclass
class FailingResource:
    group: str = ""
    kind: str = ""
    name: str = ""
    namespace: str = ""
    health: str = ""
    sync: str = ""
    message: str = ""


@dataclass
class Fix:
    route: FixRoute = "none"
    detail: str = ""


@dataclass
class Diagnosis:
    app: str
    app_kind: str  # "Application" | "Kustomization" | "HelmRelease"
    health: str = ""
    sync: str = ""
    root_cause: RootCause = "unknown"
    summary: str = ""
    first_failing: Optional[FailingResource] = None
    evidence: list[Evidence] = field(default_factory=list)
    fix: Fix = field(default_factory=Fix)

    def to_dict(self) -> dict:
        return asdict(self)
