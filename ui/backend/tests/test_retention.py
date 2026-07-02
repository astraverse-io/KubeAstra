"""Unit and integration tests for Trace Retention with Golden-Run Exemption."""

from pathlib import Path
import sys
import os
import json
import sqlite3
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import db
from agent_errors import AgentErrorType
import auth

def _init_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test-retention.db"))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    db.init_db()


def _seed_user_and_session() -> tuple[str, str]:
    user = db.create_user(username="testuser", password_hash="hash")
    db.upsert_session("session-test", user_id=user["id"])
    return user["id"], "session-test"


def test_prune_agent_runs_db_helper(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    # 1. Create a run that is 40 days old (should be pruned)
    run_old_id = db.create_agent_run(session_id=sid, user_id=uid, route="react")
    # Manually adjust started_at back in time
    with db._conn() as con:
        con.execute(
            "UPDATE agent_runs SET started_at = datetime('now', '-40 days') WHERE id = ?",
            (run_old_id,),
        )
    # Record a step for it (to test cascading delete)
    step_old_id = db.record_agent_step(
        run_id=run_old_id, iteration=1, action="get_pods", status="ok", duration_ms=10
    )

    # 2. Create a golden run that is 40 days old (should NOT be pruned)
    run_golden_id = db.create_agent_run(session_id=sid, user_id=uid, route="react", retention_policy="golden")
    with db._conn() as con:
        con.execute(
            "UPDATE agent_runs SET started_at = datetime('now', '-40 days') WHERE id = ?",
            (run_golden_id,),
        )
    step_golden_id = db.record_agent_step(
        run_id=run_golden_id, iteration=1, action="get_pods", status="ok", duration_ms=10
    )

    # 3. Create a recent run (should NOT be pruned)
    run_recent_id = db.create_agent_run(session_id=sid, user_id=uid, route="react")
    step_recent_id = db.record_agent_step(
        run_id=run_recent_id, iteration=1, action="get_pods", status="ok", duration_ms=10
    )

    # Verify everything exists initially
    assert db.get_agent_run(run_old_id) is not None
    assert db.get_agent_run(run_golden_id) is not None
    assert db.get_agent_run(run_recent_id) is not None
    
    # Run the prune helper for 30 days
    deleted = db.prune_agent_runs(retention_days=30)
    assert deleted == 1

    # Verify Run 1 is deleted, along with its step
    assert db.get_agent_run(run_old_id) is None
    with db._conn() as con:
        cur = con.execute("SELECT id FROM agent_steps WHERE run_id = ?", (run_old_id,))
        assert cur.fetchone() is None

    # Verify Golden Run and Recent Run remain untouched
    assert db.get_agent_run(run_golden_id) is not None
    assert db.get_agent_run(run_recent_id) is not None
    assert len(db.get_agent_steps(run_golden_id)) == 1
    assert len(db.get_agent_steps(run_recent_id)) == 1


def test_update_agent_run_retention_db_helper(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    run_id = db.create_agent_run(session_id=sid, user_id=uid, route="react")
    run = db.get_agent_run(run_id)
    assert run["retention_policy"] == "standard"

    # Toggle to golden
    success = db.update_agent_run_retention(run_id, "golden")
    assert success is True
    assert db.get_agent_run(run_id)["retention_policy"] == "golden"

    # Toggle back to standard
    success = db.update_agent_run_retention(run_id, "standard")
    assert success is True
    assert db.get_agent_run(run_id)["retention_policy"] == "standard"

    # Fake run should return False
    assert db.update_agent_run_retention("fake-run-id", "golden") is False


def test_prune_endpoint(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    # Create an old run
    run_old_id = db.create_agent_run(session_id=sid, user_id=uid, route="react")
    with db._conn() as con:
        con.execute(
            "UPDATE agent_runs SET started_at = datetime('now', '-40 days') WHERE id = ?",
            (run_old_id,),
        )

    # Setup environment token
    monkeypatch.setenv("PRUNE_TOKEN", "super-secret-token")

    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)

    # 1. Call prune without token -> Unauthorized (with auth disabled, endpoint falls back to allowed,
    # but since token check fails, it requires admin. If auth is disabled, require_current_user returns {}
    # and is_admin(user) is false, so it raises 403 or 401 if auth is enabled. Since we mocked AUTH_ENABLED
    # to false, auth.auth_enabled() is False, so it allows unauthenticated users if token check fails.
    # Let's test with AUTH_ENABLED=true to check the full auth enforcement).
    
    monkeypatch.setenv("AUTH_ENABLED", "true")
    # Without token and unauthenticated -> should raise 401
    response = client.post("/api/agent-runs/prune?days=30")
    assert response.status_code == 401

    # With invalid token -> should raise 401 (fails token check, falls back to auth check, unauthenticated)
    response = client.post(
        "/api/agent-runs/prune?days=30",
        headers={"X-Prune-Token": "wrong-token"}
    )
    assert response.status_code == 401

    # With valid token -> should succeed
    response = client.post(
        "/api/agent-runs/prune?days=30",
        headers={"X-Prune-Token": "super-secret-token"}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "success", "deleted_runs": 1}
    assert db.get_agent_run(run_old_id) is None


def test_toggle_golden_endpoint(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    run_id = db.create_agent_run(session_id=sid, user_id=uid, route="react")
    assert db.get_agent_run(run_id)["retention_policy"] == "standard"

    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)

    # Toggle to golden
    response = client.post(f"/api/agent-runs/{run_id}/toggle-golden")
    assert response.status_code == 200
    assert response.json() == {"status": "success", "retention_policy": "golden"}
    assert db.get_agent_run(run_id)["retention_policy"] == "golden"

    # Toggle back to standard
    response = client.post(f"/api/agent-runs/{run_id}/toggle-golden")
    assert response.status_code == 200
    assert response.json() == {"status": "success", "retention_policy": "standard"}
    assert db.get_agent_run(run_id)["retention_policy"] == "standard"

    # Toggle non-existent run -> 404
    response = client.post("/api/agent-runs/non-existent/toggle-golden")
    assert response.status_code == 404


def test_cli_prune_runs_direct(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    run_old_id = db.create_agent_run(session_id=sid, user_id=uid, route="react")
    with db._conn() as con:
        con.execute(
            "UPDATE agent_runs SET started_at = datetime('now', '-40 days') WHERE id = ?",
            (run_old_id,),
        )

    # Invoke main of scripts/prune_runs.py using importlib.util to avoid scripts package namespace conflicts
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "prune_runs",
        str(BACKEND_DIR / "scripts" / "prune_runs.py")
    )
    prune_runs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prune_runs)

    # Mock command line arguments
    test_args = ["prune_runs", "--days", "30", "--direct"]
    with patch("sys.argv", test_args):
        exit_code = prune_runs.main()
        assert exit_code == 0

    assert db.get_agent_run(run_old_id) is None
