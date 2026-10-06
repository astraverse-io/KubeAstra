"""Phase 4: GitOps Reconciliation Pilot — deterministic, keyless diagnosis.

Covers Argo CD Application + Flux Kustomization/HelmRelease status fixtures:
root-cause classification, first-failing-resource selection, fix routing, the
injected-runner fetch adapter, JSON serializability, and core purity.
See PILOTS_PLAN.md §Phase 4.
"""
import json
import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from services.reconcile import diagnose, fetch_gitops_object  # noqa: E402


def _argo(health, sync, resources=None, conditions=None, op=None, name="web"):
    return {
        "kind": "Application",
        "metadata": {"name": name},
        "status": {
            "health": {"status": health},
            "sync": {"status": sync},
            "resources": resources or [],
            "conditions": conditions or [],
            "operationState": op or {},
        },
    }


# ── Argo CD ───────────────────────────────────────────────────────────────────

def test_argo_healthy_needs_nothing():
    dx = diagnose(_argo("Healthy", "Synced"))
    assert dx.root_cause == "healthy" and dx.fix.route == "none"


def test_argo_out_of_sync_drift_offers_pr():
    dx = diagnose(_argo("Healthy", "OutOfSync"))
    assert dx.root_cause == "out_of_sync_drift"
    assert dx.fix.route == "gitops_pr"


def test_argo_image_pull_degraded_resource():
    res = [{
        "group": "apps", "kind": "Deployment", "name": "api", "namespace": "shop",
        "status": "Synced", "health": {"status": "Degraded", "message": "Back-off pulling image: ImagePullBackOff"},
    }]
    dx = diagnose(_argo("Degraded", "Synced", resources=res))
    assert dx.root_cause == "image_pull"
    assert dx.first_failing and dx.first_failing.kind == "Deployment" and dx.first_failing.name == "api"
    assert dx.fix.route == "advisory"
    assert any("ImagePullBackOff" in e.detail for e in dx.evidence)


def test_argo_crd_schema_mismatch_from_condition():
    conds = [{"type": "ComparisonError", "message": 'no matches for kind "Foo" in version "example.com/v1"'}]
    dx = diagnose(_argo("Missing", "OutOfSync", conditions=conds))
    assert dx.root_cause == "crd_schema_mismatch" and dx.fix.route == "advisory"


def test_argo_rbac_from_operation_state():
    op = {"phase": "Failed", "message": 'cannot create resource "secrets" ... is forbidden'}
    dx = diagnose(_argo("Degraded", "OutOfSync", op=op))
    assert dx.root_cause == "rbac"


def test_argo_prefers_degraded_resource_over_merely_outofsync():
    res = [
        {"kind": "ConfigMap", "name": "cm", "status": "OutOfSync", "health": {"status": "Healthy"}},
        {"kind": "Deployment", "name": "api", "status": "Synced", "health": {"status": "Degraded", "message": "CrashLoopBackOff"}},
    ]
    dx = diagnose(_argo("Degraded", "OutOfSync", resources=res))
    assert dx.first_failing.kind == "Deployment"


# ── Flux ──────────────────────────────────────────────────────────────────────

def test_flux_kustomization_not_ready_is_crd_mismatch():
    obj = {
        "kind": "Kustomization",
        "metadata": {"name": "apps"},
        "status": {"conditions": [
            {"type": "Ready", "status": "False", "reason": "BuildFailed",
             "message": "kustomize build failed: no matches for kind CronJob"},
        ]},
    }
    dx = diagnose(obj)
    assert dx.app_kind == "Kustomization" and dx.health == "Degraded"
    assert dx.root_cause == "crd_schema_mismatch"
    assert dx.first_failing and dx.first_failing.kind == "Kustomization"


def test_flux_helmrelease_ready_is_healthy():
    obj = {
        "kind": "HelmRelease",
        "metadata": {"name": "redis"},
        "status": {"conditions": [
            {"type": "Ready", "status": "True", "reason": "InstallSucceeded", "message": "Helm install succeeded"},
        ]},
    }
    dx = diagnose(obj)
    assert dx.root_cause == "healthy" and dx.health == "Healthy"


def test_diagnosis_is_json_serializable():
    json.dumps(diagnose(_argo("Degraded", "OutOfSync")).to_dict())


# ── fetch adapter (injected runner, no cluster) ───────────────────────────────

def test_fetch_argocd_app_queries_right_resource():
    captured = []

    def fake(args):
        captured.append(tuple(args))
        return {"kind": "Application", "metadata": {"name": "web"},
                "status": {"health": {"status": "Healthy"}, "sync": {"status": "Synced"}}}

    obj = fetch_gitops_object(fake, kind="Application", name="web", namespace="argocd")
    assert obj["kind"] == "Application"
    assert ("get", "applications.argoproj.io", "web", "-n", "argocd", "-o", "json") in captured


def test_fetch_flux_helmrelease_uses_right_plural():
    seen = []

    def fake(args):
        seen.append(list(args))
        return {}

    fetch_gitops_object(fake, kind="HelmRelease", name="redis", namespace="flux-system")
    flat = [p for call in seen for p in call]
    # CodeQL py/incomplete-url-substring-sanitization false positive: `flat` is the
    # list of kubectl argv tokens, and this is list-membership asserting the right
    # resource was queried — not URL parsing or a security check.
    assert "helmreleases.helm.toolkit.fluxcd.io" in flat


def test_fetch_tolerates_runner_errors():
    def boom(args):
        raise RuntimeError("no cluster")

    assert fetch_gitops_object(boom, kind="Application", name="web") == {}


# ── purity ────────────────────────────────────────────────────────────────────

def test_reconcile_core_is_pure():
    pkg = MCP_DIR / "services" / "reconcile"
    forbidden = (
        "fastapi", "pydantic", "sqlite3", "anthropic", "openai", "langchain",
        "import db", "from db", "import ui", "from ui", "requests", "httpx", "yaml",
    )
    offenders = []
    for py in sorted(pkg.glob("*.py")):
        for line in py.read_text().splitlines():
            s = line.strip().lower()
            if s.startswith("import ") or s.startswith("from "):
                for bad in forbidden:
                    if bad in s:
                        offenders.append(f"{py.name}: {line}")
    assert not offenders, "reconcile core imported something heavy:\n" + "\n".join(offenders)
