"""Case types + loaders for the Pilots eval suite.

Cases are labeled YAML fixtures under ``cases/upgrade/``, ``cases/reconcile/``
and ``cases/resilience/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class UpgradeCase:
    """A labeled upgrade scenario: a snapshot + target, with expected outputs."""

    id: str
    target: str
    snapshot: dict
    expect_blocking: tuple[str, ...]           # blocking GVKs
    expect_routes: dict                        # gvk -> expected step kind
    expect_order: tuple[str, ...]              # known-good step slug order


@dataclass(frozen=True)
class ReconcileCase:
    """A labeled GitOps status object + expected root cause."""

    id: str
    obj: dict
    expect_root_cause: str


@dataclass(frozen=True)
class ResilienceCase:
    """Manifests (as rendered YAML) + the scored rules they must trigger.

    ``expect_rules`` is the exact set of rule ids among scored findings — a
    missing one is a recall miss, an extra one a false positive. ``expect_score``
    and ``expect_fixes`` (rule -> fix kind) are checked when given.
    """

    id: str
    manifests: str
    source_mode: str
    facts: dict
    expect_rules: frozenset[str]
    expect_score: Optional[int]
    expect_fixes: dict


def _resilience_from_dict(d: dict) -> ResilienceCase:
    for k in ("id", "manifests", "expect"):
        if k not in d:
            raise ValueError(f"resilience case missing key: {k}")
    exp = d["expect"]
    if "rules" not in exp:
        raise ValueError(f"resilience case {d['id']!r}: expect.rules is required (use [] for a clean case)")
    return ResilienceCase(
        id=str(d["id"]),
        manifests=str(d["manifests"]),
        source_mode=str(d.get("source_mode", "static")),
        facts=dict(d.get("facts") or {}),
        expect_rules=frozenset(exp.get("rules") or ()),
        expect_score=exp.get("score"),
        expect_fixes=dict(exp.get("fixes") or {}),
    )


def _upgrade_from_dict(d: dict) -> UpgradeCase:
    for k in ("id", "target", "snapshot", "expect"):
        if k not in d:
            raise ValueError(f"upgrade case missing key: {k}")
    exp = d["expect"]
    return UpgradeCase(
        id=str(d["id"]),
        target=str(d["target"]),
        snapshot=d["snapshot"],
        expect_blocking=tuple(exp.get("blocking") or ()),
        expect_routes=dict(exp.get("routes") or {}),
        expect_order=tuple(exp.get("order") or ()),
    )


def _reconcile_from_dict(d: dict) -> ReconcileCase:
    for k in ("id", "obj", "expect_root_cause"):
        if k not in d:
            raise ValueError(f"reconcile case missing key: {k}")
    return ReconcileCase(id=str(d["id"]), obj=d["obj"], expect_root_cause=str(d["expect_root_cause"]))


def _load_dir(subdir: str, builder):
    import yaml

    base = Path(__file__).resolve().parent / "cases" / subdir
    out = []
    seen: set[str] = set()
    for path in sorted(base.glob("*.yaml")):
        with path.open("r", encoding="utf-8") as fh:
            case = builder(yaml.safe_load(fh))
        if case.id in seen:
            raise ValueError(f"duplicate case id {case.id!r} ({path.name})")
        seen.add(case.id)
        out.append(case)
    return out


def load_upgrade_cases() -> list[UpgradeCase]:
    return _load_dir("upgrade", _upgrade_from_dict)


def load_reconcile_cases() -> list[ReconcileCase]:
    return _load_dir("reconcile", _reconcile_from_dict)


def load_resilience_cases() -> list[ResilienceCase]:
    return _load_dir("resilience", _resilience_from_dict)
