"""Score the keyless Pilot cores against labeled cases (deterministic).

Runs the real ``planner.assess``/``planner.plan`` and ``reconcile.diagnose`` and
compares to the case labels. Because the cores are deterministic, a correct suite
scores perfectly — so a drop here is a genuine regression in detection, routing,
ordering, or root-cause classification.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.reconcile import diagnose
from services.upgrade import planner
from services.upgrade.snapshot import ClusterSnapshot, HelmRelease, ObjectRef

from .metrics import accuracy, kendall_tau, recall_precision
from .types import ReconcileCase, UpgradeCase


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
