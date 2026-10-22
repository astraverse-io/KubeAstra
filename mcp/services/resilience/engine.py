"""``assess``: documents in, :class:`ResilienceReport` out. Deterministic, keyless."""
from __future__ import annotations

from typing import Any, Iterable, Optional

from .catalogue import apply_config, load_rules
from .fix import build_fix
from .model import build_inventory, objects_from_docs
from .rules import RULES, Ctx
from .score import score_workloads
from .types import ClusterFacts, Finding, ResilienceReport


def assess(
    items: Iterable[Any],
    *,
    facts: Optional[ClusterFacts] = None,
    source_mode: str = "static",
    config: Optional[dict[str, Any]] = None,
    catalogue: Optional[dict[str, Any]] = None,
) -> ResilienceReport:
    """Assess manifests (``ManifestDoc``s or plain dicts).

    ``source_mode="live"`` means the docs came from a cluster: wiring findings
    (RES013/RES014) stop being advisory, and ``facts`` (nodes, zones) sharpen
    RES005. ``config`` is a repo's ``.kubeastra/resilience.yaml`` as a dict.
    """
    if source_mode not in ("static", "live"):
        raise ValueError("source_mode must be 'static' or 'live'")
    cat = apply_config(catalogue if catalogue is not None else load_rules(), config)
    facts = facts or ClusterFacts()
    inv = build_inventory(objects_from_docs(items))
    ctx = Ctx(inv=inv, facts=facts, static=source_mode == "static")

    findings: list[Finding] = []
    for rid, fn in RULES.items():
        meta = cat["rules"][rid]
        if not meta.get("enabled", True):
            continue
        for hit in fn(ctx):
            findings.append(
                Finding(
                    rule=rid,
                    severity=meta["severity"],
                    group=meta["group"],
                    title=meta["title"],
                    message=hit.message,
                    objects=[o.ref for o in hit.objs],
                    advisory=hit.advisory,
                    suppressed=any(o.ignores(rid) for o in hit.objs),
                    fix=build_fix(hit, ctx),
                )
            )

    score, grade, workloads = score_workloads(inv, findings, cat)
    notes: list[str] = []
    if not inv.workloads:
        notes.append("No workloads (Deployment, StatefulSet, DaemonSet, Job, CronJob) found in the input.")
    if ctx.static:
        notes.append(
            "Static mode: Service/PDB wiring checks are advisory (the app may span other directories), "
            "and node/zone counts are unknown. Run with --live for the cluster's view."
        )
    return ResilienceReport(
        score=score,
        grade=grade,
        workloads=workloads,
        findings=findings,
        source_mode=source_mode,
        facts=facts,
        notes=notes,
    )
