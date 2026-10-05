"""Deterministic Upgrade Pilot planner — pure and keyless.

    assess(snapshot, target, maps) -> ReadinessReport   (Phase 1: real logic)
    plan(report, maps)            -> Plan                (Phase 2: skeleton)

``assess`` matches the in-use GVKs in a :class:`ClusterSnapshot` against the
API-deprecation map (the GUARANTEED core), adds ADVISORY operator-compat hints,
and evaluates the provider version-skew policy. ``plan`` (Phase 2) will turn the
report into an ordered, per-item-routed migration plan.

Invariant for every phase: **no LLM, no DB, no ``ui/`` imports.** The optional
natural-language narration is a separate, caller-side concern; the report and
plan are always computed here, deterministically.
"""
from __future__ import annotations

from typing import Any, Optional

from .maps import Deprecation, minor_le, minor_tuple
from .scan import split_api_version
from .snapshot import ClusterSnapshot, ObjectRef
from .types import BlockingFinding, OperatorFinding, Plan, ReadinessReport, SkewFinding


def _gvk_parts(obj: ObjectRef) -> tuple[str, str, str]:
    """Recover (group, version, kind) from an ObjectRef whose ``gvk`` is
    ``"<apiVersion>/<kind>"`` (apiVersion may itself contain a '/')."""
    api_version, _, _kind = obj.gvk.rpartition("/")
    group, version = split_api_version(api_version)
    return group, version, obj.kind


def _index(deprecations: list[Deprecation]) -> dict[tuple[str, str, str], Deprecation]:
    return {d.key: d for d in deprecations}


def assess(
    snapshot: ClusterSnapshot,
    target: str,
    maps: Optional[dict[str, Any]] = None,
) -> ReadinessReport:
    """Assess what breaks moving ``snapshot`` to ``target`` (e.g. "1.31").

    GUARANTEED: ``blocking`` is derived from the API-deprecation map — an in-use
    object using an apiVersion removed at or before ``target``. ADVISORY:
    operator-compat hints (live mode only) and provider skew.
    """
    if not target:
        raise ValueError("assess() requires a target version, e.g. '1.31'")
    minor_tuple(target)  # validate early

    if maps is None:
        from .maps import load_maps

        maps = load_maps()

    index = _index(maps.get("deprecations") or [])

    blocking: list[BlockingFinding] = []
    deprecated_notes: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for obj in snapshot.objects:
        group, version, kind = _gvk_parts(obj)
        dep = index.get((group, version, kind))
        if dep is None:
            continue
        if minor_le(dep.removed_in, target):
            dedupe = (obj.gvk, obj.namespace, obj.name)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            blocking.append(
                BlockingFinding(
                    gvk=dep.api_version + "/" + kind,
                    kind=kind,
                    name=obj.name,
                    namespace=obj.namespace,
                    source=obj.source,
                    replacement=dep.replacement,
                    advisory=False,
                )
            )
        elif dep.deprecated_in and minor_le(dep.deprecated_in, target):
            # Deprecated but not yet removed at the target — a heads-up, not a blocker.
            where = f"{obj.namespace}/{obj.name}" if obj.namespace else obj.name
            deprecated_notes.append(
                f"{dep.api_version}/{kind} ({where}) is deprecated as of {dep.deprecated_in} "
                f"(removed in {dep.removed_in}); migrate to {dep.replacement or 'its successor'}."
            )

    operators = _assess_operators(snapshot, target, maps)
    skew = _assess_skew(snapshot, target, maps)

    report = ReadinessReport(
        current=snapshot.cluster_version,
        target=target,
        provider=snapshot.provider or "unknown",
        blocking=blocking,
        operators=operators,
        skew=skew,
        notes=deprecated_notes,
    )
    if snapshot.source_mode == "static":
        report.notes.append(
            "Static mode: API-deprecation findings are authoritative; "
            "operator-compat advice requires a live cluster and is skipped."
        )
    return report


def _assess_operators(
    snapshot: ClusterSnapshot, target: str, maps: dict[str, Any]
) -> list[OperatorFinding]:
    """ADVISORY operator-compat hints. Live mode only — static scans can't see
    installed operators reliably, so we don't guess."""
    if snapshot.source_mode == "static":
        return []
    compat = (maps.get("operator_compat") or {}).get("operators") or []
    by_group = {}
    for entry in compat:
        g = ((entry or {}).get("detect") or {}).get("crd_group")
        if g:
            by_group[g] = entry

    findings: list[OperatorFinding] = []
    for op in snapshot.operators:
        # Operators carry CRD *groups* (e.g. "cert-manager.io"); match on those,
        # then fall back to matching by operator name.
        entry = None
        for crd_group in op.crds:
            if crd_group in by_group:
                entry = by_group[crd_group]
                break
        if entry is None:
            entry = by_group.get(op.name)
        if entry is None:
            continue
        min_by_k8s = entry.get("min_version_by_k8s") or {}
        required = _min_required_for_target(min_by_k8s, target)
        action = "unknown"
        if required and op.version:
            action = "ok" if _ver_ge(op.version, required) else "bump"
        findings.append(
            OperatorFinding(
                name=entry.get("name", op.name),
                installed=op.version,
                min_required=required or "",
                action=action,
                advisory=True,
            )
        )
    return findings


def _assess_skew(snapshot: ClusterSnapshot, target: str, maps: dict[str, Any]) -> SkewFinding:
    skew_cfg = (maps.get("provider_eol") or {}).get("skew") or {}
    try:
        t_major, t_minor = minor_tuple(target)
    except ValueError:
        return SkewFinding(ok=True, detail="")
    allowed = skew_cfg.get("max_node_minor_behind_default", 2)
    if (t_major, t_minor) >= (1, 28):
        allowed = skew_cfg.get("max_node_minor_behind_since_1_28", allowed)

    worst_behind = 0
    for v in snapshot.node_kubelet_versions:
        try:
            _, n_minor = minor_tuple(v)
        except ValueError:
            continue
        worst_behind = max(worst_behind, t_minor - n_minor)
    if not snapshot.node_kubelet_versions:
        return SkewFinding(ok=True, detail="No node versions available to check skew.")
    ok = worst_behind <= allowed
    detail = (
        f"Nodes trail the target by up to {worst_behind} minor(s); "
        f"policy allows {allowed}."
    )
    return SkewFinding(ok=ok, detail=detail)


def _min_required_for_target(min_by_k8s: dict, target: str) -> Optional[str]:
    """Highest operator version floor whose k8s key is <= target."""
    best_key: Optional[tuple[int, int]] = None
    best_val: Optional[str] = None
    for k8s_ver, op_ver in min_by_k8s.items():
        try:
            kt = minor_tuple(str(k8s_ver))
        except ValueError:
            continue
        if minor_le(str(k8s_ver), target) and (best_key is None or kt > best_key):
            best_key, best_val = kt, str(op_ver)
    return best_val


def _ver_ge(a: str, b: str) -> bool:
    """Compare dotted versions numerically: a >= b. Non-numeric tails ignored."""
    def parts(v: str) -> tuple[int, ...]:
        out = []
        for seg in v.strip().lstrip("vV").split("."):
            num = ""
            for ch in seg:
                if ch.isdigit():
                    num += ch
                else:
                    break
            out.append(int(num) if num else 0)
        return tuple(out)

    pa, pb = parts(a), parts(b)
    width = max(len(pa), len(pb))
    pa += (0,) * (width - len(pa))
    pb += (0,) * (width - len(pb))
    return pa >= pb


def plan(report: ReadinessReport, maps: Optional[dict[str, Any]] = None) -> Plan:
    """Turn a :class:`ReadinessReport` into an ordered migration :class:`Plan`.

    Phase 0/1 return an empty, well-formed plan. Phase 2 adds the deterministic
    ordering and per-item remedy routing.
    """
    return Plan(current=report.current, target=report.target, steps=[])
