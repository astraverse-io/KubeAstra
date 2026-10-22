"""Resilience Pilot Phase 3: the server side — the pilot run, the
assess_resilience tool, the namespace allowlist, and node drains on the
upgrade run. kubectl is faked; no cluster.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for _p in (BACKEND_DIR, MCP_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
import pilots as pilots_registry  # noqa: E402

PAYMENTS = MCP_DIR / "tests" / "fixtures" / "resilience" / "payments"


def _reset_settings_cache():
    from config.settings import get_settings

    try:
        get_settings.cache_clear()
    except Exception:
        pass


def _deployment(ns, name, replicas=2):
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns},
            "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}},
                                  "spec": {"containers": [{"name": "c", "image": "x:1.0.0"}]}}}}


def _pdb(ns, name, min_available):
    return {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {"name": name, "namespace": ns},
            "spec": {"minAvailable": min_available, "selector": {"matchLabels": {"app": name}}}}


class FakeRunner:
    """Answers `kubectl get <kinds> (-n NS | --all-namespaces)` and `get nodes`."""

    def __init__(self, objects, fail=None):
        self.objects = objects
        self.fail = fail
        self.calls = []

    def run_json(self, args, namespace=None):
        self.calls.append(list(args))
        if self.fail:
            from k8s.kubectl_runner import KubectlError

            raise KubectlError(self.fail, 1, self.fail)
        if args[:2] == ["get", "nodes"]:
            return {"items": [{"metadata": {"labels": {}}, "spec": {}}] * 3}
        items = self.objects
        if "-n" in args:
            ns = args[args.index("-n") + 1]
            items = [o for o in items if o["metadata"]["namespace"] == ns]
        return {"apiVersion": "v1", "kind": "List", "items": items}


CLUSTER = [_deployment("shop", "web"), _pdb("shop", "web", 2), _deployment("kube-system", "coredns"),
           _deployment("other", "api")]


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.setenv("PILOTS_ENABLED", "true")
    monkeypatch.setenv("RESILIENCE_PILOT_ENABLED", "true")
    monkeypatch.setenv("UPGRADE_PILOT_ENABLED", "true")
    monkeypatch.setenv("UPGRADE_PILOT_MANIFESTS_ROOT", str(tmp_path))
    _reset_settings_cache()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "res.db"))
    db.init_db()
    from main import app

    yield TestClient(app)
    _reset_settings_cache()


def _fake(monkeypatch, runner):
    import k8s.kubectl_runner as kr
    import k8s.wrappers as wr

    monkeypatch.setattr(kr, "get_runner", lambda: runner)
    monkeypatch.setattr(wr, "get_runner", lambda: runner)


def _allow(monkeypatch, namespaces):
    monkeypatch.setenv("ALLOWED_NAMESPACES", namespaces)
    _reset_settings_cache()


# ── registry ──────────────────────────────────────────────────────────────────

def test_resilience_pilot_is_registered_and_flag_gated():
    from types import SimpleNamespace

    p = pilots_registry.get_pilot("resilience")
    assert p is not None and p.flag == "resilience_pilot_enabled" and not p.write_capable
    assert "assess_resilience" in p.tool_scope
    assert p not in pilots_registry.enabled_pilots(SimpleNamespace(resilience_pilot_enabled=False))


# ── the pilot run ─────────────────────────────────────────────────────────────

def test_static_run_assesses_and_persists(app_client, tmp_path):
    shutil.copytree(PAYMENTS, tmp_path / "payments")
    r = app_client.post("/api/v1/pilots/resilience/run", json={"inputs": {"manifests_path": "payments"}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "assessed"
    assert body["report"]["score"] == 77 and body["report"]["grade"] == "C"
    res001 = next(f for f in body["report"]["findings"] if f["rule"] == "RES001")
    assert res001["fix"]["gitops"]["field_path"] == ["spec", "minAvailable"]  # ready for /api/gitops/preview
    got = app_client.get(f"/api/v1/pilots/runs/{body['run_id']}").json()
    assert got["status"] == "assessed" and got["plan"]["report"]["score"] == 77


def test_static_run_path_escape_is_a_400_not_a_500(app_client):
    r = app_client.post("/api/v1/pilots/resilience/run", json={"inputs": {"manifests_path": "../../etc"}})
    assert r.status_code == 400


def test_live_run_with_wildcard_allowlist_is_one_call_and_skips_system(app_client, monkeypatch):
    _allow(monkeypatch, "*")
    runner = FakeRunner(CLUSTER)
    _fake(monkeypatch, runner)
    body = app_client.post("/api/v1/pilots/resilience/run", json={}).json()
    names = {f"{w['ref']['namespace']}/{w['ref']['name']}" for w in body["report"]["workloads"]}
    assert names == {"shop/web", "other/api"}
    assert "--all-namespaces" in runner.calls[0]
    assert any("kube-system" in n for n in body["report"]["notes"])
    assert {f["rule"] for f in body["report"]["findings"]} >= {"RES001"}


def test_live_run_with_an_allowlist_reads_only_allowed_namespaces(app_client, monkeypatch):
    _allow(monkeypatch, "shop")
    runner = FakeRunner(CLUSTER)
    _fake(monkeypatch, runner)
    body = app_client.post("/api/v1/pilots/resilience/run", json={}).json()
    assert {w["ref"]["name"] for w in body["report"]["workloads"]} == {"web"}
    gets = [c for c in runner.calls if c[:2] != ["get", "nodes"]]
    assert gets and all("--all-namespaces" not in c and c[c.index("-n") + 1] == "shop" for c in gets)


def test_live_run_namespace_must_be_allowed(app_client, monkeypatch):
    _allow(monkeypatch, "shop")
    _fake(monkeypatch, FakeRunner(CLUSTER))
    r = app_client.post("/api/v1/pilots/resilience/run", json={"inputs": {"namespace": "other"}})
    assert r.status_code == 422 and "not in the allowed list" in r.text


def test_live_run_kubectl_failure_fails_the_run(app_client, monkeypatch):
    _allow(monkeypatch, "*")
    _fake(monkeypatch, FakeRunner(CLUSTER, fail="Unauthorized"))
    r = app_client.post("/api/v1/pilots/resilience/run", json={})
    assert r.status_code == 500 and "Unauthorized" in r.text


def test_upgrade_run_reports_node_drains(app_client, tmp_path):
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "app.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: web, namespace: shop}\n"
        "spec:\n  replicas: 2\n  template:\n    metadata: {labels: {app: web}}\n"
        "    spec: {containers: [{name: c, image: 'x:1.0.0'}]}\n---\n"
        "apiVersion: policy/v1\nkind: PodDisruptionBudget\nmetadata: {name: web, namespace: shop}\n"
        "spec: {minAvailable: 2, selector: {matchLabels: {app: web}}}\n"
    )
    body = app_client.post("/api/v1/pilots/upgrade/run", json={"target": "1.31", "inputs": {"manifests_path": "m"}}).json()
    assert body["status"] == "planned"
    assert body["drains"]["checked"] and [f["rule"] for f in body["drains"]["findings"]] == ["RES001"]


def test_upgrade_run_still_plans_when_the_drain_check_cannot_run(app_client, monkeypatch):
    _allow(monkeypatch, "*")
    _fake(monkeypatch, FakeRunner([], fail="connection refused"))
    import routers.pilots as rp

    monkeypatch.setattr(rp, "_build_snapshot", lambda body: __import__("services.upgrade", fromlist=["x"]).ClusterSnapshot())
    body = app_client.post("/api/v1/pilots/upgrade/run", json={"target": "1.31"}).json()
    assert body["status"] == "planned"
    assert body["drains"]["checked"] is False and "connection refused" in body["drains"]["note"]


# ── the assess_resilience tool ────────────────────────────────────────────────

def test_tool_is_registered_on_every_surface():
    import tool_registry

    t = tool_registry.TOOLS["assess_resilience"]
    assert t.surfaces == frozenset({"mcp", "chat", "react"}) and not t.write_op


def test_tool_output_is_compact_and_worst_first(monkeypatch):
    _allow(monkeypatch, "*")
    _fake(monkeypatch, FakeRunner(CLUSTER))
    from k8s.wrappers import assess_resilience

    out = assess_resilience()
    assert out["scope"] == "all namespaces" and out["nodes"] == 3
    assert out["findings"][0]["severity"] == "high" and out["findings"][0]["rule"] == "RES001"
    assert out["findings"][0]["fix"] == "field" and "minAvailable: 1" in out["findings"][0]["fix_note"]
    assert out["workloads"][0]["workload"] == "Deployment shop/web"  # the lowest score first
    assert not out["truncated"]
    _reset_settings_cache()


def test_tool_namespace_respects_the_allowlist(monkeypatch):
    _allow(monkeypatch, "shop")
    _fake(monkeypatch, FakeRunner(CLUSTER))
    from k8s.validators import ValidationError
    from k8s.wrappers import assess_resilience

    with pytest.raises(ValidationError):
        assess_resilience(namespace="other")
    assert assess_resilience(namespace="shop")["scope"] == "namespace shop"
    _reset_settings_cache()
