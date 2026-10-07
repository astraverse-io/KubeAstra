"""Phase 0: the Pilot framework — flag gating, tool-scope validity, router gate.

See ``internal_docs/features/PILOTS_PLAN.md`` §Phase 0.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[1]  # .../ui/backend
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for _p in (BACKEND_DIR, MCP_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import pilots as pilots_registry  # noqa: E402


def _stub_settings(**flags):
    base = {"upgrade_pilot_enabled": False, "gitops_reconcile_enabled": False}
    base.update(flags)
    return SimpleNamespace(**base)


def _reset_settings_cache():
    from config.settings import get_settings

    try:
        get_settings.cache_clear()
    except Exception:
        pass


# ── framework unit tests (no app, no DB) ──────────────────────────────────────

def test_registry_has_v1_pilots():
    assert {"upgrade", "gitops_reconcile"} <= set(pilots_registry.REGISTRY)


def test_enabled_pilots_respects_flags():
    assert pilots_registry.enabled_pilots(_stub_settings()) == []

    only_upgrade = pilots_registry.enabled_pilots(_stub_settings(upgrade_pilot_enabled=True))
    assert {p.name for p in only_upgrade} == {"upgrade"}

    both = pilots_registry.enabled_pilots(
        _stub_settings(upgrade_pilot_enabled=True, gitops_reconcile_enabled=True)
    )
    assert {p.name for p in both} == {"upgrade", "gitops_reconcile"}


def test_tool_scopes_are_valid_react_tools():
    from tool_registry import valid_tool_names

    valid = set(valid_tool_names("react"))
    for p in pilots_registry.REGISTRY.values():
        unknown = set(p.tool_scope) - valid
        assert not unknown, f"pilot {p.name} scopes unknown tools: {sorted(unknown)}"


# ── router tests (feature-gate + run lifecycle) ───────────────────────────────

def test_router_404_when_master_flag_off(monkeypatch):
    monkeypatch.setenv("PILOTS_ENABLED", "false")
    _reset_settings_cache()
    from main import app

    assert TestClient(app).get("/api/v1/pilots").status_code == 404
    _reset_settings_cache()


def test_router_lists_only_enabled_pilots(monkeypatch, tmp_path):
    monkeypatch.setenv("PILOTS_ENABLED", "true")
    monkeypatch.setenv("UPGRADE_PILOT_ENABLED", "true")
    monkeypatch.setenv("GITOPS_RECONCILE_ENABLED", "false")
    _reset_settings_cache()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "pilots.db"))
    db.init_db()
    from main import app

    r = TestClient(app).get("/api/v1/pilots")
    assert r.status_code == 200, r.text
    body = r.json()
    assert {p["name"] for p in body["pilots"]} == {"upgrade"}
    up = next(p for p in body["pilots"] if p["name"] == "upgrade")
    assert up["tool_scope"] and up["write_capable"] is False
    _reset_settings_cache()


def test_upgrade_run_produces_and_persists_a_plan(monkeypatch, tmp_path):
    monkeypatch.setenv("PILOTS_ENABLED", "true")
    monkeypatch.setenv("UPGRADE_PILOT_ENABLED", "true")
    monkeypatch.setenv("UPGRADE_PILOT_MANIFESTS_ROOT", str(tmp_path))
    _reset_settings_cache()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "pilots-run.db"))
    db.init_db()

    # A manifest with a deprecated API — scanned keylessly via manifests_path
    # (STATIC mode, no cluster/kubectl needed).
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "ing.yaml").write_text(
        "apiVersion: networking.k8s.io/v1beta1\nkind: Ingress\n"
        "metadata: {name: web, namespace: shop}\n"
    )

    from main import app

    c = TestClient(app)
    r = c.post(
        "/api/v1/pilots/upgrade/run",
        json={"target": "1.22", "inputs": {"manifests_path": str(manifests)}},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "planned"
    assert body["report"]["blocking"], "expected the deprecated Ingress to be flagged"
    assert any(s["kind"] == "manifest_pr" for s in body["plan"]["steps"])
    assert body["plan"]["steps"][-1]["kind"] == "control_plane"

    # the stored run carries the plan back
    got = c.get(f"/api/v1/pilots/runs/{body['run_id']}")
    assert got.status_code == 200
    assert got.json()["status"] == "planned"
    assert got.json()["plan"]["plan"]["steps"]

    # the upgrade pilot requires a target
    assert c.post("/api/v1/pilots/upgrade/run", json={}).status_code == 422
    # a disabled pilot is not runnable
    assert c.post("/api/v1/pilots/gitops_reconcile/run", json={}).status_code == 404
    _reset_settings_cache()


def test_gitops_reconcile_run_diagnoses(monkeypatch, tmp_path):
    monkeypatch.setenv("PILOTS_ENABLED", "true")
    monkeypatch.setenv("GITOPS_RECONCILE_ENABLED", "true")
    _reset_settings_cache()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "recon.db"))
    db.init_db()

    from main import app

    c = TestClient(app)
    # An OutOfSync Argo app supplied directly (no cluster) via inputs.object.
    app_obj = {
        "kind": "Application",
        "metadata": {"name": "web"},
        "status": {"health": {"status": "Healthy"}, "sync": {"status": "OutOfSync"}},
    }
    r = c.post("/api/v1/pilots/gitops_reconcile/run", json={"inputs": {"object": app_obj}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "diagnosed"
    assert body["diagnosis"]["root_cause"] == "out_of_sync_drift"
    assert body["diagnosis"]["fix"]["route"] == "gitops_pr"

    got = c.get(f"/api/v1/pilots/runs/{body['run_id']}")
    assert got.status_code == 200
    assert got.json()["plan"]["diagnosis"]["root_cause"] == "out_of_sync_drift"

    # requires inputs.app or inputs.object
    assert c.post("/api/v1/pilots/gitops_reconcile/run", json={}).status_code == 422
    _reset_settings_cache()


def test_enabled_pilot_without_run_impl_returns_501(monkeypatch, tmp_path):
    # Review fix #1: a registered + enabled pilot with no run branch must not be
    # mis-routed into another pilot's pipeline — it gets a clean 501.
    monkeypatch.setenv("PILOTS_ENABLED", "true")
    monkeypatch.setenv("UPGRADE_PILOT_ENABLED", "true")  # reuse to enable the fake pilot
    _reset_settings_cache()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "noimpl.db"))
    db.init_db()

    from pilots import REGISTRY, Pilot, register

    register(Pilot(name="migration", tool_scope=frozenset(), narration_prompt="x",
                   flag="upgrade_pilot_enabled"))
    try:
        from main import app

        r = TestClient(app).post("/api/v1/pilots/migration/run", json={})
        assert r.status_code == 501, r.text
    finally:
        REGISTRY.pop("migration", None)
    _reset_settings_cache()


def test_plan_approval_authorizes_non_high_risk_steps(monkeypatch, tmp_path):
    # Phase 5 Option B: one admin decision authorizes the non-high-risk steps;
    # high-risk steps are refused here (they're approved individually).
    monkeypatch.setenv("PILOTS_ENABLED", "true")
    monkeypatch.setenv("UPGRADE_PILOT_ENABLED", "true")
    monkeypatch.setenv("UPGRADE_PILOT_MANIFESTS_ROOT", str(tmp_path))
    _reset_settings_cache()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "approve.db"))
    db.init_db()
    manifests = tmp_path / "m"
    manifests.mkdir()
    (manifests / "ing.yaml").write_text(
        "apiVersion: networking.k8s.io/v1beta1\nkind: Ingress\n"
        "metadata: {name: web, namespace: shop}\n"
    )

    from main import app

    c = TestClient(app)
    run = c.post(
        "/api/v1/pilots/upgrade/run",
        json={"target": "1.22", "inputs": {"manifests_path": str(manifests)}},
    ).json()
    run_id = run["run_id"]
    steps = run["plan"]["steps"]
    low = [s["id"] for s in steps if s["risk"] != "high"]
    high = [s["id"] for s in steps if s["risk"] == "high"]
    assert low and high, "expected both a manifest_pr (low) and control_plane (high) step"

    r = c.post(f"/api/v1/remediation/plans/{run_id}/approve", json={"step_ids": low})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["step_ids"]) == sorted(low)
    assert c.get(f"/api/v1/pilots/runs/{run_id}").json()["auth_state"] == "plan_authorized"

    # high-risk steps cannot be bulk plan-approved
    assert c.post(f"/api/v1/remediation/plans/{run_id}/approve", json={"step_ids": high}).status_code == 422
    # unknown step id / empty / missing run
    assert c.post(f"/api/v1/remediation/plans/{run_id}/approve", json={"step_ids": ["nope"]}).status_code == 422
    assert c.post(f"/api/v1/remediation/plans/{run_id}/approve", json={"step_ids": []}).status_code == 422
    assert c.post("/api/v1/remediation/plans/missing/approve", json={"step_ids": low}).status_code == 404
    _reset_settings_cache()


def test_gitops_reconcile_rejects_unsafe_live_inputs(monkeypatch, tmp_path):
    # Review fix #2: app/namespace/kind flow into kubectl args on the live path.
    monkeypatch.setenv("PILOTS_ENABLED", "true")
    monkeypatch.setenv("GITOPS_RECONCILE_ENABLED", "true")
    _reset_settings_cache()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "recon2.db"))
    db.init_db()

    from main import app

    c = TestClient(app)
    # flag-injection-looking app name
    assert c.post("/api/v1/pilots/gitops_reconcile/run", json={"inputs": {"app": "--all"}}).status_code == 422
    # bad namespace
    assert c.post(
        "/api/v1/pilots/gitops_reconcile/run", json={"inputs": {"app": "web", "namespace": "-x"}}
    ).status_code == 422
    # unknown kind
    assert c.post(
        "/api/v1/pilots/gitops_reconcile/run", json={"inputs": {"app": "web", "kind": "Foo"}}
    ).status_code == 422
    _reset_settings_cache()


# ── manifests_path containment guard (CodeQL py/path-injection) ────────────────

def test_manifests_path_disabled_by_default(monkeypatch, tmp_path):
    import routers.pilots as pr
    monkeypatch.setattr(pr, "_settings", lambda: SimpleNamespace(upgrade_pilot_manifests_root=""))
    with pytest.raises(pr.HTTPException) as ei:
        pr._safe_manifests_path("anything")
    assert ei.value.status_code == 400 and "disabled" in ei.value.detail


def test_manifests_path_allows_inside_root(monkeypatch, tmp_path):
    import routers.pilots as pr
    (tmp_path / "manifests").mkdir()
    monkeypatch.setattr(pr, "_settings", lambda: SimpleNamespace(upgrade_pilot_manifests_root=str(tmp_path)))
    resolved = pr._safe_manifests_path("manifests")
    assert resolved == str((tmp_path / "manifests").resolve())


def test_manifests_path_rejects_absolute_escape(monkeypatch, tmp_path):
    import routers.pilots as pr
    monkeypatch.setattr(pr, "_settings", lambda: SimpleNamespace(upgrade_pilot_manifests_root=str(tmp_path)))
    with pytest.raises(pr.HTTPException) as ei:
        pr._safe_manifests_path("/etc")
    assert ei.value.status_code == 400 and "escapes" in ei.value.detail


def test_manifests_path_rejects_dotdot_traversal(monkeypatch, tmp_path):
    import routers.pilots as pr
    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setattr(pr, "_settings", lambda: SimpleNamespace(upgrade_pilot_manifests_root=str(base)))
    with pytest.raises(pr.HTTPException):
        pr._safe_manifests_path("../../etc/passwd")
