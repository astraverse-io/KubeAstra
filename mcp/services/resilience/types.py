"""Typed results for the Resilience Pilot core.

Pure dataclasses, stdlib only — no ``ui/``, no LLM, no DB. ``to_dict`` gives the
JSON shape the server, CLI and Action return.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

Severity = Literal["low", "medium", "high"]
FixKind = Literal["field", "new_object", "block", "advisory"]


@dataclass(frozen=True)
class Ref:
    """Where an object came from. ``file``/``line`` are empty in live mode."""

    kind: str
    name: str
    namespace: str = ""
    file: str = ""
    line: int = 0

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.namespace, self.name)


@dataclass
class ClusterFacts:
    """What live mode knows about the cluster. ``None`` = unknown (static mode)."""

    nodes: Optional[int] = None
    zones: Optional[list[str]] = None


@dataclass
class Fix:
    """How to fix a finding (built by ``fix.py``).

    - ``field``: change one existing value. ``yaml`` is a strategic-merge patch;
      ``gitops`` is the change for ``POST /api/gitops/preview`` when a PR can make it.
    - ``new_object``: ``yaml`` is a whole new object (e.g. a PodDisruptionBudget).
    - ``block``: add a block that isn't there yet. ``yaml`` is a strategic-merge patch.
    - ``advisory``: needs a human — a value only you know, or a change that isn't
      safe to make blind. ``yaml`` may hold a suggested starting point; it is
      never applied by ``--fix``.
    """

    kind: FixKind
    target: dict[str, Any] = field(default_factory=dict)  # kind, name, namespace, apiVersion, file, line
    field_path: tuple = ()
    new_value: Any = None
    yaml: str = ""
    note: str = ""
    gitops: Optional[dict[str, Any]] = None


@dataclass
class Finding:
    rule: str
    severity: Severity
    group: str
    title: str
    message: str
    objects: list[Ref] = field(default_factory=list)  # every object involved; workload first when there is one
    advisory: bool = False  # static-mode uncertainty — reported, never scored
    suppressed: bool = False  # opted out by annotation or repo config — reported, never scored
    fix: Optional[Fix] = None

    @property
    def scored(self) -> bool:
        return not (self.advisory or self.suppressed)


@dataclass
class WorkloadScore:
    ref: Ref
    score: int
    grade: str
    rules: list[str] = field(default_factory=list)  # ids of the scored findings that hit it


@dataclass
class ResilienceReport:
    """Deterministic for a given input + rules file. No LLM.

    ``score``/``grade`` are ``None``/``"-"`` when the input has no workloads —
    "nothing to assess" must not read as a perfect score.
    """

    score: Optional[int] = None
    grade: str = "-"
    workloads: list[WorkloadScore] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    source_mode: str = "static"
    facts: ClusterFacts = field(default_factory=ClusterFacts)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
