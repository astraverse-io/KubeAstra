"""Score the keyless Pilot cores against labeled cases (deterministic).

Runs the real ``planner.assess``/``planner.plan`` and ``reconcile.diagnose`` and
compares to the case labels. Because the cores are deterministic, a correct suite
scores perfectly — so a drop here is a genuine regression in detection, routing,
ordering, or root-cause classification.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from services.reconcile import diagnose
from services.resilience import ClusterFacts
from services.resilience import assess as assess_resilience
from services.upgrade import planner
from services.upgrade.snapshot import ClusterSnapshot, HelmRelease, ObjectRef

from .metrics import accuracy, kendall_tau, recall_precision
from .types import ReconcileCase, ResilienceCase, UpgradeCase


def snapshot_from_dict(d: dict) -> ClusterSnapshot:
    """Build a ClusterSnapshot from a case's ``snapshot`` mapping."""
    return ClusterSnapshot(
        cluster_version=d.get("cluster_version", ""),
        provider=d.get("provider", "unknown"),
        source_mode=d.get("source_mode", "static"),
        node_kubelet_versions=list(d.get("node_kubelet_versions") or []),
        objects=[
            ObjectRef(
                gvk=o["gvk"],
                kind=o.get("kind", ""),
                name=o.get("name", ""),
                namespace=o.get("namespace", ""),
                source=o.get("source", "manifest"),
            )
            for o in (d.get("objects") or [])
        ],
        helm_releases=[
            HelmRelease(
                name=h["name"],
                namespace=h.get("namespace", ""),
                chart=h.get("chart", ""),
                chart_version=h.get("chart_version", ""),
                app_version=h.get("app_version", ""),
            )
            for h in (d.get("helm_releases") or [])
        ],
    )


def _step_slug(step) -> str:
    """Stable identifier for ordering: the targeted GVK, else the step kind."""
    return (step.target or {}).get("gvk") or step.kind


@dataclass
class UpgradeScore:
    case_id: str
    recall: float
    precision: float
    routing_accuracy: float
    order_tau: float

    @property
    def perfect(self) -> bool:
        return (
            self.recall == 1.0
            and self.precision == 1.0
            and self.routing_accuracy == 1.0
            and self.order_tau == 1.0
        )


def score_upgrade(case: UpgradeCase) -> UpgradeScore:
    snap = snapshot_from_dict(case.snapshot)
    report = planner.assess(snap, case.target)
    found = [b.gvk for b in report.blocking]
    recall, precision = recall_precision(found, case.expect_blocking)

    plan = planner.plan(report)
    produced_route = {
        (s.target or {}).get("gvk"): s.kind for s in plan.steps if (s.target or {}).get("gvk")
    }
    route_flags = [produced_route.get(gvk) == kind for gvk, kind in case.expect_routes.items()]
    routing_accuracy = accuracy(route_flags)

    produced_order = [_step_slug(s) for s in sorted(plan.steps, key=lambda x: x.order)]
    order_tau = kendall_tau(produced_order, list(case.expect_order))

    return UpgradeScore(
        case_id=case.id,
        recall=recall,
        precision=precision,
        routing_accuracy=routing_accuracy,
        order_tau=order_tau,
    )


@dataclass
class ReconcileScore:
    case_id: str
    hit: bool
    produced: str
    expected: str


def score_reconcile(case: ReconcileCase) -> ReconcileScore:
    diag = diagnose(case.obj)
    return ReconcileScore(
        case_id=case.id,
        hit=diag.root_cause == case.expect_root_cause,
        produced=diag.root_cause,
        expected=case.expect_root_cause,
    )


@dataclass
class ResilienceScore:
    case_id: str
    found: frozenset
    expected: frozenset
    recall: float
    precision: float
    score: object
    score_ok: bool
    fix_mismatches: list

    @property
    def perfect(self) -> bool:
        return self.recall == 1.0 and self.precision == 1.0 and self.score_ok and not self.fix_mismatches


def score_resilience(case: ResilienceCase) -> ResilienceScore:
    docs = [d for d in yaml.safe_load_all(case.manifests) if d]
    facts = ClusterFacts(**case.facts) if case.facts else None
    report = assess_resilience(docs, facts=facts, source_mode=case.source_mode)
    scored = [f for f in report.findings if f.scored]
    found = frozenset(f.rule for f in scored)
    recall, precision = recall_precision(found, case.expect_rules)
    mismatches = []
    for rule, kind in case.expect_fixes.items():
        kinds = {f.fix.kind for f in scored if f.rule == rule and f.fix}
        if kind not in kinds:
            mismatches.append(f"{rule}: expected fix {kind}, got {sorted(kinds) or 'none'}")
    return ResilienceScore(
        case_id=case.id,
        found=found,
        expected=case.expect_rules,
        recall=recall,
        precision=precision,
        score=report.score,
        score_ok=case.expect_score is None or report.score == case.expect_score,
        fix_mismatches=mismatches,
    )


def per_rule(scores: list[ResilienceScore]) -> dict[str, tuple[float, float, int]]:
    """rule -> (precision, recall, support) across every case. A rule that
    fires where it isn't expected loses precision; one that stays silent where
    it is expected loses recall."""
    rules = set().union(*(s.found | s.expected for s in scores)) if scores else set()
    out = {}
    for rule in sorted(rules):
        tp = sum(1 for s in scores if rule in s.found and rule in s.expected)
        fp = sum(1 for s in scores if rule in s.found and rule not in s.expected)
        fn = sum(1 for s in scores if rule not in s.found and rule in s.expected)
        precision = tp / (tp + fp) if tp + fp else 1.0
        recall = tp / (tp + fn) if tp + fn else 1.0
        out[rule] = (precision, recall, tp + fn)
    return out
