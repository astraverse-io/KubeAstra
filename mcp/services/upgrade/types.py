"""Typed results for the Upgrade Pilot core.

Pure dataclasses, stdlib only — no third-party, no ``ui/``, no LLM, no DB.
``to_dict`` gives a JSON-serializable shape the server/CLI/Action can return.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

Severity = Literal["low", "medium", "high"]
RemedyRoute = Literal["gitops_pr", "remediation", "advisory"]
StepKind = Literal["manifest_pr", "helm_bump", "operator_bump", "control_plane", "verify"]


@dataclass
class BlockingFinding:
    """An in-use object whose apiVersion is deprecated/removed at the target."""

    gvk: str
    kind: str
    name: str
    namespace: str = ""
    source: str = "manifest"  # "manifest" | "helm" | "operator"
    replacement: Optional[str] = None
    advisory: bool = False  # True = heuristic (operator-compat), not guaranteed


@dataclass
class OperatorFinding:
    """A compatibility hint for an installed operator. Always advisory in v1."""

    name: str
    installed: str = ""
    min_required: str = ""
    action: str = "unknown"  # "bump" | "ok" | "unknown"
    advisory: bool = True


@dataclass
class SkewFinding:
    """Control-plane vs node version-skew assessment against provider policy."""

    ok: bool = True
    detail: str = ""


@dataclass
class ReadinessReport:
    """What breaks on ``target``. Produced deterministically, no LLM."""

    current: str = ""
    target: str = ""
    provider: str = "unknown"
    blocking: list[BlockingFinding] = field(default_factory=list)
    operators: list[OperatorFinding] = field(default_factory=list)
    skew: SkewFinding = field(default_factory=SkewFinding)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Step:
    """One ordered step of a migration plan. Applies nothing by itself."""

    id: str
    order: int
    title: str
    kind: StepKind
    target: dict[str, Any] = field(default_factory=dict)
    change: dict[str, Any] = field(default_factory=dict)
    remedy_route: RemedyRoute = "advisory"
    risk: Severity = "low"
    reversible: bool = True
    advisory: bool = False
    verify: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


@dataclass
class Plan:
    """An ordered, per-item-routed migration plan. Deterministic."""

    current: str = ""
    target: str = ""
    steps: list[Step] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
