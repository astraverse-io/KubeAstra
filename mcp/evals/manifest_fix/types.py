"""Data types for the manifest-fix eval suite.

A :class:`Case` is one golden task loaded from ``cases/<id>.yaml``; a
:class:`ScoreResult` is the objective breakdown of scoring a candidate fix.
Both are plain, keyless value types so the suite stays deterministic and
importable without the backend app.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_UNSET = object()


@dataclass(frozen=True)
class Check:
    """One objective assertion a correct fix must satisfy.

    Exactly one of ``equals`` / ``exists`` is meaningful. ``path`` is a dotted
    path with optional list indices, e.g. ``spec.template.spec.containers[0].image``.
    """

    path: str
    equals: Any = _UNSET
    exists: Optional[bool] = None

    @property
    def mode(self) -> str:
        if self.equals is not _UNSET:
            return "equals"
        if self.exists is not None:
            return "exists"
        return "invalid"


@dataclass(frozen=True)
class Case:
    """One golden fix-this-manifest task."""

    id: str
    symptom: str
    kind: str
    broken: str  # the broken manifest (YAML text)
    fixed: str   # the known-good fix (YAML text)
    checks: tuple[Check, ...] = ()


@dataclass
class ScoreResult:
    """Objective score of a candidate fix against a :class:`Case`."""

    score: float
    parses: bool
    schema_valid: bool
    checks_passed: int
    checks_total: int
    detail: str = ""


def _check_from_dict(d: dict) -> Check:
    return Check(
        path=d["path"],
        equals=d["equals"] if "equals" in d else _UNSET,
        exists=d.get("exists"),
    )


def case_from_dict(d: dict) -> Case:
    """Build a :class:`Case` from a loaded YAML mapping."""
    missing = [k for k in ("id", "symptom", "kind", "broken", "fixed") if k not in d]
    if missing:
        raise ValueError(f"case missing required keys: {missing}")
    checks = tuple(_check_from_dict(c) for c in (d.get("checks") or []))
    return Case(
        id=str(d["id"]),
        symptom=str(d["symptom"]),
        kind=str(d["kind"]),
        broken=str(d["broken"]),
        fixed=str(d["fixed"]),
        checks=checks,
    )


def load_cases(cases_dir: Optional[Path] = None) -> list[Case]:
    """Load every ``cases/<id>.yaml`` under ``cases_dir`` (default: ./cases).

    Sorted by id for stable reporting. Raises if two files declare the same id.
    """
    import yaml

    base = cases_dir or (Path(__file__).resolve().parent / "cases")
    cases: list[Case] = []
    seen: set[str] = set()
    for path in sorted(base.glob("*.yaml")):
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        case = case_from_dict(data)
        if case.id in seen:
            raise ValueError(f"duplicate case id {case.id!r} (file {path.name})")
        seen.add(case.id)
        cases.append(case)
    return sorted(cases, key=lambda c: c.id)
