"""KubeAstra Upgrade Pilot — pure planner core.

This package is **dependency-free by design**: it imports nothing from ``ui/``,
no LLM client, no database, no web framework. It takes a :class:`ClusterSnapshot`
(plain data a caller builds however it likes — a live cluster, rendered
manifests, or a test fixture) plus the deprecation/compat maps, and returns a
deterministic :class:`ReadinessReport` and :class:`Plan`.

Keeping it pure is what lets the server, the CLI, and the GitHub Action all share
one core, and what makes it testable with zero infra. See
``internal_docs/features/PILOTS_PLAN.md`` §Phase 0–2.

Phase 0 ships the seam and the types; Phases 1–2 fill in ``assess``/``plan`` with
the real API-deprecation map, operator-compat hints, provider skew, and the
deterministic ordering + per-item remedy routing.
"""
from .types import (
    BlockingFinding,
    OperatorFinding,
    Plan,
    ReadinessReport,
    SkewFinding,
    Step,
)
from .snapshot import ClusterSnapshot, HelmRelease, ObjectRef, OperatorInfo
from .planner import assess, plan
from .scan import scan_cluster, scan_manifests, snapshot_from_cluster
from .maps import Deprecation, load_maps, minor_le, minor_tuple, validate_api_deprecations

__all__ = [
    "assess",
    "plan",
    "scan_manifests",
    "scan_cluster",
    "snapshot_from_cluster",
    "load_maps",
    "validate_api_deprecations",
    "minor_le",
    "minor_tuple",
    "Deprecation",
    "ClusterSnapshot",
    "ObjectRef",
    "HelmRelease",
    "OperatorInfo",
    "ReadinessReport",
    "BlockingFinding",
    "OperatorFinding",
    "SkewFinding",
    "Plan",
    "Step",
]
