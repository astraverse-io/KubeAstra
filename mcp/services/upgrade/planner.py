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

import re
from typing import Any, Optional

from .maps import Deprecation, minor_le, minor_tuple
from .scan import split_api_version
from .snapshot import ClusterSnapshot, ObjectRef
from .types import BlockingFinding, OperatorFinding, Plan, ReadinessReport, SkewFinding, Step


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


# ── Plan construction (Phase 2) ──────────────────────────────────────────────

# Ordering rank by step kind — lower runs earlier. An operator that owns a
# deprecated CRD is bumped before the manifests/CRs using the old apiVersions;
# CRDs migrate before the CRs that depend on them; the control-plane bump is last.
_ORDER = {
    "operator_bump": 0,
    "helm_bump": 1,
    "manifest_pr_crd": 2,
    "manifest_pr": 3,
    "control_plane": 4,
}
_CRD_KIND = "CustomResourceDefinition"


def _slug(*parts: str) -> str:
    raw = ":".join(p for p in parts if p)
    return re.sub(r"[^a-zA-Z0-9]+", "-", raw).strip("-").lower() or "step"


def _api_version_of(gvk: str) -> str:
    """"networking.k8s.io/v1beta1/Ingress" -> "networking.k8s.io/v1beta1"."""
    return gvk.rpartition("/")[0]


def _route_blocking(b: BlockingFinding) -> dict[str, Any]:
    """Route a blocking object to a remedy and score its risk (deterministic).

    Plain-YAML/Kustomize -> a reviewed GitOps PR (apiVersion swap). Helm- or
    operator-managed objects are fixed by bumping the chart/operator that owns
    them, NOT by editing the rendered manifest (#81's boundary). A removal with
    no in-place replacement needs human rework, not a mechanical swap.
    """
    if b.source == "helm":
        return {"kind": "helm_bump", "remedy_route": "remediation", "risk": "medium",
                "reversible": True, "advisory": False}
    if b.source == "operator":
        return {"kind": "operator_bump", "remedy_route": "remediation", "risk": "medium",
                "reversible": True, "advisory": False}
    if b.replacement is None:
        return {"kind": "manifest_pr", "remedy_route": "advisory", "risk": "high",
                "reversible": False, "advisory": False}
    return {"kind": "manifest_pr", "remedy_route": "gitops_pr", "risk": "low",
            "reversible": True, "advisory": False}


def _control_plane_commands(provider: str, target: str) -> list[str]:
    p = (provider or "unknown").lower()
    if p == "eks":
        return [
            f"eksctl upgrade cluster --name <cluster> --version {target} --approve",
            f"eksctl upgrade nodegroup --cluster <cluster> --name <nodegroup> --kubernetes-version {target}",
        ]
    if p == "gke":
        return [
            f"gcloud container clusters upgrade <cluster> --master --cluster-version {target}",
            f"gcloud container clusters upgrade <cluster> --node-pool <pool> --cluster-version {target}",
        ]
    if p == "aks":
        return [
            f"az aks upgrade --resource-group <rg> --name <cluster> --kubernetes-version {target}",
        ]
    return [
        f"# Upgrade the control plane to {target} one minor at a time (e.g. kubeadm upgrade apply v{target}),",
        "# then drain and upgrade each node pool, staying within the version-skew policy.",
    ]


def _control_plane_rationale(report: ReadinessReport) -> str:
    base = (
        "Advisory: run after every object/operator step has landed. KubeAstra does not "
        "execute control-plane changes (it holds no cloud credentials)."
    )
    if not report.skew.ok and report.skew.detail:
        base += f" WARNING — node skew: {report.skew.detail}"
    return base


def _blocking_rationale(b: BlockingFinding, route: dict[str, Any]) -> str:
    if route["kind"] == "helm_bump":
        return (
            f"{b.gvk} is removed at the target and is rendered by a Helm chart; bump the chart "
            f"(the release owns the manifest), do not edit the rendered output directly."
        )
    if route["kind"] == "operator_bump":
        return f"{b.gvk} is owned by an operator; migrate via the operator/CR, not a direct edit."
    if b.replacement is None:
        return (
            f"{b.gvk} is removed at the target with no in-place replacement; this needs a human "
            f"rework (e.g. PodSecurityPolicy → Pod Security Admission), not an apiVersion swap."
        )
    return f"{b.gvk} is removed at the target; migrate the manifest to {b.replacement} via a reviewed PR."


def plan(report: ReadinessReport, maps: Optional[dict[str, Any]] = None) -> Plan:
    """Turn a :class:`ReadinessReport` into an ordered, per-item-routed migration
    :class:`Plan`. Deterministic and keyless — no LLM, no cluster. The optional
    natural-language narration is a separate, caller-side concern.
    """
    pending: list[tuple[tuple, Step]] = []

    # 1. Operator bumps (advisory) — first; they own the deprecated CRDs.
    for op in report.operators:
        if op.action != "bump":
            continue
        pending.append((
            (_ORDER["operator_bump"], op.name, ""),
            Step(
                id=_slug("operator", op.name),
                order=0,
                title=f"Bump operator {op.name} to >= {op.min_required or 'a compatible version'}",
                kind="operator_bump",
                target={"operator": op.name},
                change={"from": op.installed, "to_min": op.min_required},
                remedy_route="remediation",
                risk="medium",
                reversible=True,
                advisory=True,
                verify={"check": "operator_version", "operator": op.name, "min": op.min_required},
                rationale=(
                    "Operator owns deprecated CRD apiVersions; bump it before migrating the "
                    "objects it manages. ADVISORY — operator-compat is a hint; verify manually."
                ),
            ),
        ))

    # 2. Blocking objects → routed steps.
    for b in report.blocking:
        route = _route_blocking(b)
        is_crd = b.kind == _CRD_KIND
        rank = (
            _ORDER["manifest_pr_crd"]
            if route["kind"] == "manifest_pr" and is_crd
            else _ORDER[route["kind"]]
        )
        where = f"{b.namespace}/{b.name}" if b.namespace else (b.name or "<unnamed>")
        if route["kind"] == "manifest_pr":
            title = f"Migrate {b.gvk} ({where}) → {b.replacement or 'replacement (manual)'}"
            change: dict[str, Any] = {"before": _api_version_of(b.gvk), "after": b.replacement}
        elif route["kind"] == "helm_bump":
            title = f"Bump the Helm chart rendering {b.gvk} ({where})"
            change = {"release_hint": where, "reason": f"chart renders removed {b.gvk}"}
        else:
            title = f"Update the operator/CR owning {b.gvk} ({where})"
            change = {"owner_hint": where}
        pending.append((
            (rank, b.gvk, where),
            Step(
                id=_slug(route["kind"], b.gvk, where),
                order=0,
                title=title,
                kind=route["kind"],
                target={"gvk": b.gvk, "name": b.name, "namespace": b.namespace, "source": b.source},
                change=change,
                remedy_route=route["remedy_route"],
                risk=route["risk"],
                reversible=route["reversible"],
                advisory=route["advisory"],
                verify={"check": "api_absent", "gvk": b.gvk, "name": b.name, "namespace": b.namespace},
                rationale=_blocking_rationale(b, route),
            ),
        ))

    # 3. Control-plane bump (advisory) — always last.
    pending.append((
        (_ORDER["control_plane"], "", ""),
        Step(
            id=_slug("control-plane", report.target),
            order=0,
            title=f"Upgrade control plane + node pools to {report.target}",
            kind="control_plane",
            target={"target_version": report.target, "provider": report.provider},
            change={"commands": _control_plane_commands(report.provider, report.target)},
            remedy_route="advisory",
            risk="high",
            reversible=False,
            advisory=True,
            verify={"check": "cluster_version", "expect": report.target},
            rationale=_control_plane_rationale(report),
        ),
    ))

    pending.sort(key=lambda t: t[0])
    steps: list[Step] = []
    for i, (_rank, step) in enumerate(pending, start=1):
        step.order = i
        steps.append(step)
    return Plan(current=report.current, target=report.target, steps=steps)
