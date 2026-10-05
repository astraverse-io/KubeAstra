"""Deterministic Upgrade Pilot planner — pure and keyless.

    assess(snapshot, target, maps) -> ReadinessReport
    plan(report, maps)            -> Plan

Phase 0 ships the signatures and a deterministic skeleton so the seam, the API,
and the tests are real. The actual logic lands in later phases:

- **Phase 1** — ``assess`` matches in-use GVKs against the auto-generated
  ``api_deprecations`` map, adds advisory operator-compat hints, and evaluates
  provider version-skew.
- **Phase 2** — ``plan`` produces the ordered steps with per-item remedy routing
  (plain-YAML → PR, Helm/operator-managed → chart bump, control-plane → advisory).

Invariant for every phase: **no LLM, no DB, no ``ui/`` imports.** The optional
natural-language narration is a *separate*, caller-side concern; the plan itself
is always computed here, deterministically.
"""
from __future__ import annotations

from typing import Any, Optional

from .snapshot import ClusterSnapshot
from .types import Plan, ReadinessReport, SkewFinding


def assess(
    snapshot: ClusterSnapshot,
    target: str,
    maps: Optional[dict[str, Any]] = None,
) -> ReadinessReport:
    """Assess what breaks when moving ``snapshot`` to ``target`` (e.g. "1.31").

    Phase 0 returns a structured, empty report (no deprecation data wired yet).
    The signature and shape are stable; Phase 1 fills ``blocking``/``operators``/
    ``skew`` from ``maps``.
    """
    if not target:
        raise ValueError("assess() requires a target version, e.g. '1.31'")

    report = ReadinessReport(
        current=snapshot.cluster_version,
        target=target,
        provider=snapshot.provider or "unknown",
        blocking=[],  # Phase 1: matched against the api_deprecations map
        operators=[],  # Phase 1: advisory operator-compat hints
        skew=SkewFinding(ok=True, detail=""),  # Phase 1: provider skew policy
    )
    if maps is None:
        report.notes.append(
            "Phase 0 skeleton: deprecation/compat evaluation lands in Phase 1."
        )
    if snapshot.source_mode == "static":
        report.notes.append(
            "Static mode: API-deprecation findings are authoritative; "
            "operator-compat advice requires a live cluster and is skipped."
        )
    return report


def plan(report: ReadinessReport, maps: Optional[dict[str, Any]] = None) -> Plan:
    """Turn a :class:`ReadinessReport` into an ordered migration :class:`Plan`.

    Phase 0 returns an empty, well-formed plan. Phase 2 adds the deterministic
    ordering and per-item remedy routing.
    """
    return Plan(
        current=report.current,
        target=report.target,
        steps=[],  # Phase 2: deterministic ordering + per-item remedy routing
    )
