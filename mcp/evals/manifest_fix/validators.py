"""Objective scorers for candidate manifest fixes (design §7).

Pure functions, no LLM and no network: given a candidate manifest (YAML text)
and a :class:`Case`, produce a :class:`ScoreResult` in [0, 1]. The score is the
mean of three kinds of signal — it parses, it is schema-shaped, and each of the
case's checks passes — with parsing as a hard gate (unparseable ⇒ 0.0).

This is the deterministic half of the eval; an LLM judge (run.py, LIVE only)
adds the subjective "is the fix right and minimal" signal on top.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import yaml

from .types import Case, Check, ScoreResult

# Kinds whose spec must carry a pod template with at least one container.
_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob"}

_INDEX_RE = re.compile(r"^([^\[\]]+)\[(\d+)\]$")


def parse_manifest(text: str) -> Optional[dict]:
    """Return the first YAML document as a dict, or None if it won't parse.

    Multi-doc files return their first mapping document (the resource under test).
    Anything that isn't a mapping, or that raises, yields None.
    """
    try:
        docs = [d for d in yaml.safe_load_all(text) if d is not None]
    except yaml.YAMLError:
        return None
    for doc in docs:
        if isinstance(doc, dict):
            return doc
    return None


def is_schema_valid(doc: dict) -> bool:
    """Minimal structural validity for a Kubernetes resource.

    Not a full OpenAPI validation — just the invariants every manifest needs
    (apiVersion/kind/metadata.name), plus a pod template with ≥1 named container
    for workload kinds. Enough to catch fixes that dropped required structure.
    """
    if not isinstance(doc, dict):
        return False
    if not isinstance(doc.get("apiVersion"), str) or not doc["apiVersion"].strip():
        return False
    kind = doc.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        return False
    meta = doc.get("metadata")
    if not isinstance(meta, dict) or not isinstance(meta.get("name"), str) or not meta["name"].strip():
        return False

    if kind in _WORKLOAD_KINDS:
        containers = resolve_path(doc, "spec.template.spec.containers")
        if kind == "CronJob":
            containers = resolve_path(doc, "spec.jobTemplate.spec.template.spec.containers")
        if not isinstance(containers, list) or not containers:
            return False
        for c in containers:
            if not isinstance(c, dict) or not c.get("name") or not c.get("image"):
                return False
    return True


def resolve_path(doc: Any, path: str) -> Any:
    """Resolve a dotted path with optional list indices against ``doc``.

    e.g. ``spec.template.spec.containers[0].image``. Returns ``None`` if any
    segment is missing or an index is out of range. ``None`` is therefore
    indistinguishable from a literal null — fine for these checks, which assert
    concrete values or existence of non-null fields.
    """
    cur = doc
    for segment in path.split("."):
        if cur is None:
            return None
        key, index = segment, None
        m = _INDEX_RE.match(segment)
        if m:
            key, index = m.group(1), int(m.group(2))
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
        if index is not None:
            if not isinstance(cur, list) or index >= len(cur):
                return None
            cur = cur[index]
    return cur


def check_passes(doc: dict, check: Check) -> bool:
    """Evaluate a single :class:`Check` against a parsed manifest."""
    value = resolve_path(doc, check.path)
    if check.mode == "equals":
        return value == check.equals
    if check.mode == "exists":
        present = value is not None
        return present if check.exists else not present
    return False


def score_fix(candidate_text: str, case: Case) -> ScoreResult:
    """Score a candidate fix for ``case`` in [0, 1].

    Parsing is a hard gate (unparseable ⇒ 0.0). Otherwise the score is the mean
    of ``schema_valid`` and each check result, so a fix that is well-formed and
    satisfies every check scores 1.0, and partial fixes earn partial credit.
    """
    doc = parse_manifest(candidate_text)
    if doc is None:
        return ScoreResult(
            score=0.0,
            parses=False,
            schema_valid=False,
            checks_passed=0,
            checks_total=len(case.checks),
            detail="candidate did not parse as YAML",
        )

    schema_ok = is_schema_valid(doc)
    check_results = [check_passes(doc, c) for c in case.checks]
    passed = sum(1 for r in check_results if r)

    # Always ≥1 component (schema_ok), so no empty-guard needed.
    components = [schema_ok] + check_results
    score = sum(1 for c in components if c) / len(components)

    return ScoreResult(
        score=score,
        parses=True,
        schema_valid=schema_ok,
        checks_passed=passed,
        checks_total=len(case.checks),
        detail=f"schema_valid={schema_ok} checks={passed}/{len(case.checks)}",
    )
