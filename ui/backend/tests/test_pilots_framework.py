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


def test_run_is_recorded_and_readable(monkeypatch, tmp_path):
    monkeypatch.setenv("PILOTS_ENABLED", "true")
    monkeypatch.setenv("UPGRADE_PILOT_ENABLED", "true")
    _reset_settings_cache()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "pilots-run.db"))
    db.init_db()
    from main import app

    c = TestClient(app)
    created = c.post("/api/v1/pilots/upgrade/run", json={"target": "1.31"})
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]

    got = c.get(f"/api/v1/pilots/runs/{run_id}")
    assert got.status_code == 200
    assert got.json()["pilot"] == "upgrade"
    assert got.json()["target"] == "1.31"

    # the upgrade pilot requires a target
    assert c.post("/api/v1/pilots/upgrade/run", json={}).status_code == 422
    # a disabled pilot is not runnable
    assert c.post("/api/v1/pilots/gitops_reconcile/run", json={}).status_code == 404
    _reset_settings_cache()
