"""Phase 2: deterministic, keyless plan() — ordering + per-item remedy routing.

Covers: empty-report -> control-plane only; manifest/helm/operator/no-replacement
routing; operator-before-manifest-before-control-plane ordering; CRD-before-CR;
sequential orders; JSON serializability; skew warning; and assess()->plan()
end-to-end keyless. See PILOTS_PLAN.md §Phase 2.
"""
import json
import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from services.upgrade import (  # noqa: E402
    ClusterSnapshot,
    ObjectRef,
    assess,
    load_maps,
    plan,
)
from services.upgrade.types import (  # noqa: E402
    BlockingFinding,
    OperatorFinding,
    ReadinessReport,
    SkewFinding,
)

MAPS = load_maps()


def _report(**kw) -> ReadinessReport:
    base = dict(current="1.24", target="1.25", provider="eks")
    base.update(kw)
    return ReadinessReport(**base)


def _block(gvk, kind, source="manifest", replacement="x/v1", name="n", ns="ns"):
    return BlockingFinding(gvk=gvk, kind=kind, name=name, namespace=ns, source=source, replacement=replacement)


def test_empty_report_yields_only_control_plane():
    p = plan(_report(blocking=[], operators=[]))
    assert [s.kind for s in p.steps] == ["control_plane"]
    cp = p.steps[0]
    assert cp.advisory is True and cp.reversible is False and cp.change["commands"]


def test_manifest_blocking_routes_to_reviewed_pr():
    b = _block("networking.k8s.io/v1beta1/Ingress", "Ingress", "manifest", "networking.k8s.io/v1", "web", "shop")
    step = next(s for s in plan(_report(blocking=[b])).steps if s.kind == "manifest_pr")
    assert step.remedy_route == "gitops_pr"
    assert step.risk == "low" and step.reversible is True
    assert step.change == {"before": "networking.k8s.io/v1beta1", "after": "networking.k8s.io/v1"}


def test_helm_blocking_routes_to_chart_bump():
    b = _block("batch/v1beta1/CronJob", "CronJob", "helm", "batch/v1", "nightly", "ops")
    step = next(s for s in plan(_report(blocking=[b])).steps if s.kind == "helm_bump")
    assert step.remedy_route == "remediation" and step.risk == "medium"


def test_operator_managed_blocking_routes_to_operator():
    b = _block("monitoring.coreos.com/v1/Prometheus", "Prometheus", "operator", "monitoring.coreos.com/v1", "p", "mon")
    step = next(s for s in plan(_report(blocking=[b])).steps if s.kind == "operator_bump")
    assert step.remedy_route == "remediation"


def test_removed_without_replacement_is_high_risk_manual():
    b = _block("policy/v1beta1/PodSecurityPolicy", "PodSecurityPolicy", "manifest", None, "psp", "")
    step = next(s for s in plan(_report(blocking=[b])).steps if s.kind == "manifest_pr")
    assert step.risk == "high" and step.reversible is False and step.remedy_route == "advisory"


def test_ordering_operator_before_manifest_before_control_plane():
    ops = [OperatorFinding(name="cert-manager", installed="1.9.0", min_required="1.13.0", action="bump")]
    b = _block("batch/v1beta1/CronJob", "CronJob", "manifest", "batch/v1")
    kinds = [s.kind for s in plan(_report(operators=ops, blocking=[b])).steps]
    assert kinds.index("operator_bump") < kinds.index("manifest_pr") < kinds.index("control_plane")


def test_crd_migrates_before_other_manifests():
    crd = _block("apiextensions.k8s.io/v1beta1/CustomResourceDefinition", "CustomResourceDefinition",
                 "manifest", "apiextensions.k8s.io/v1", "widgets", "")
    ing = _block("networking.k8s.io/v1beta1/Ingress", "Ingress", "manifest", "networking.k8s.io/v1", "web", "shop")
    p = plan(_report(blocking=[ing, crd]))  # CRD intentionally second in input
    manifest_gvks = [s.target["gvk"] for s in p.steps if s.kind == "manifest_pr"]
    assert manifest_gvks[0].endswith("CustomResourceDefinition")


def test_orders_sequential_and_control_plane_last():
    b = _block("batch/v1beta1/CronJob", "CronJob", "manifest", "batch/v1")
    p = plan(_report(blocking=[b]))
    assert [s.order for s in p.steps] == list(range(1, len(p.steps) + 1))
    assert p.steps[-1].kind == "control_plane"


def test_only_bump_action_operators_become_steps():
    ops = [
        OperatorFinding(name="ok-op", installed="2.0.0", min_required="1.0.0", action="ok"),
        OperatorFinding(name="old-op", installed="1.0.0", min_required="2.0.0", action="bump"),
    ]
    kinds = [s.target.get("operator") for s in plan(_report(operators=ops)).steps if s.kind == "operator_bump"]
    assert kinds == ["old-op"]


def test_plan_is_json_serializable():
    b = _block("batch/v1beta1/CronJob", "CronJob", "manifest", "batch/v1")
    json.dumps(plan(_report(blocking=[b])).to_dict())


def test_skew_warning_surfaces_in_control_plane_rationale():
    rep = _report(skew=SkewFinding(ok=False, detail="Nodes trail the target by up to 6 minor(s); policy allows 3."))
    cp = next(s for s in plan(rep).steps if s.kind == "control_plane")
    assert "WARNING" in cp.rationale and "skew" in cp.rationale.lower()


def test_provider_specific_control_plane_commands():
    for provider, needle in (("eks", "eksctl"), ("gke", "gcloud"), ("aks", "az aks"), ("unknown", "kubeadm")):
        cp = next(s for s in plan(_report(provider=provider)).steps if s.kind == "control_plane")
        assert any(needle in c for c in cp.change["commands"])


def test_end_to_end_assess_then_plan_keyless():
    snap = ClusterSnapshot(
        cluster_version="1.24",
        provider="eks",
        objects=[ObjectRef(gvk="batch/v1beta1/CronJob", kind="CronJob", name="c", namespace="ops", source="manifest")],
    )
    p = plan(assess(snap, "1.25", MAPS), MAPS)
    assert any(s.kind == "manifest_pr" for s in p.steps)
    assert p.steps[-1].kind == "control_plane"
