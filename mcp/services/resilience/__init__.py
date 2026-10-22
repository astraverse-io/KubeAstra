"""Resilience Pilot — pure core.

"Will this survive a bad day?" A deterministic, keyless score for rendered
manifests (or live cluster objects) that finds the configuration which turns a
node drain, a traffic spike or a rollout into an outage. No LLM, no ``ui/``, no
DB — the server, the ``kubeastra resilience`` CLI and the GitHub Action all
share it. See ``internal_docs/features/RESILIENCE_PILOT_PLAN.md``.

    from services.resilience import assess, scan_path
    report = assess(scan_path("rendered/"))
"""
from __future__ import annotations

from pathlib import Path

from ..manifests import ManifestDoc, iter_manifest_docs
from .catalogue import apply_config, load_rules
from .engine import assess
from .fix import MARKER as FIX_MARKER
from .fix import render_fixes
from .live import LIVE_KINDS, facts_from_nodes
from .model import IGNORE_ANNOTATION
from .rules import RULES
from .types import ClusterFacts, Finding, Fix, Ref, ResilienceReport, WorkloadScore


def scan_path(path: str | Path) -> list[ManifestDoc]:
    """Every manifest document under ``path`` (malformed files skipped)."""
    return list(iter_manifest_docs(path))


__all__ = [
    "ClusterFacts",
    "FIX_MARKER",
    "Finding",
    "Fix",
    "IGNORE_ANNOTATION",
    "LIVE_KINDS",
    "ManifestDoc",
    "RULES",
    "Ref",
    "ResilienceReport",
    "WorkloadScore",
    "apply_config",
    "assess",
    "facts_from_nodes",
    "load_rules",
    "render_fixes",
    "scan_path",
]
