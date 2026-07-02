"""Unit/integration tests for human-in-the-loop safety approvals workflow (Phase 3)."""

from pathlib import Path
import sys
import json
import time

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import db
from react import react_loop, ReActStep, ReActResult

class SequencedProvider:
    def __init__(self, responses):
        self.responses = list(responses)

    def generate_stream(self, prompt, system=None, temperature=0.1, max_tokens=8000):
        assert self.responses, "provider called more times than expected"
        response = self.responses.pop(0)
        for char in response:
            yield char


def _init_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "safety-approvals-test.db"))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    db.init_db()


def _seed_user_and_session() -> tuple[str, str]:
    user = db.create_user(username="testuser", password_hash="hash")
    db.upsert_session("session-test", user_id=user["id"])
    return user["id"], "session-test"


def test_mutating_tool_interception_and_resume(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    # Seed the user message so resume can find the question
    db.save_message(sid, "user", "delete the bad pod nginx")

    # Step 1: Run the loop requesting a mutating tool. Confirm is NOT passed, so it should suspend.
    provider1 = SequencedProvider([
        '{"thought":"I need to delete pod nginx to fix it","action":"delete_pod","params":{"namespace":"default","pod_name":"nginx"}}',
    ])

    dispatched = []

    def fake_dispatch(tool, params):
        dispatched.append((tool, params.copy()))
        if tool in ("get_pods", "get_events"):
            return {"success": True, "pods": [], "events": []}
        if params.get("dry_run"):
            return {
                "success": True,
                "dry_run": True,
                "preview": "Would delete pod nginx in namespace default",
                "confirmation_token": "token-xyz-123"
            }
        elif params.get("confirm"):
            return {
                "success": True,
                "message": "pod nginx deleted successfully"
            }
        return {"success": False, "error": "confirm required"}

    events1 = []

    # Let's create the run recorder
    from agent_run_recorder import AgentRunRecorder
    recorder1 = AgentRunRecorder.start(
        session_id=sid,
        user_id=uid,
        route="react",
        model="gemini-fake",
    )
    run_id = recorder1.run_id

    result1 = react_loop(
        question="delete the bad pod nginx",
        history=[],
        provider=provider1,
        dispatch_fn=fake_dispatch,
        on_event=events1.append,
        run_recorder=recorder1,
    )

    # Assert suspension occurred
    assert result1.error == "PendingApproval"
    assert result1.answer == "[Operation delete_pod requires human approval]"
    assert len(dispatched) == 1
    assert dispatched[0][0] == "delete_pod"
    assert dispatched[0][1].get("dry_run") is True

    # Assert database states
    run_db = db.get_agent_run(run_id)
    assert run_db["status"] == "suspended"

    steps_db = db.get_agent_steps(run_id)
    assert len(steps_db) == 1
    pending_step = steps_db[0]
    assert pending_step["status"] == "pending_approval"
    assert pending_step["action"] == "delete_pod"
    assert pending_step["observation_preview"] == "Would delete pod nginx in namespace default"

    # Assert SSE event emitted
    approval_events = [e for e in events1 if e.get("type") == "approval_required"]
    assert len(approval_events) == 1
    assert approval_events[0]["run_id"] == run_id
    assert approval_events[0]["step_id"] == pending_step["id"]
    assert approval_events[0]["confirmation_token"] == "token-xyz-123"
    assert approval_events[0]["dry_run_preview"] == "Would delete pod nginx in namespace default"

    # Step 2: Resume the loop passing the approved token!
    provider2 = SequencedProvider([
        "Verification report: nginx pod was successfully deleted.",
        '{"thought":"Pod was deleted successfully, so I can answer.","action":"answer","answer":"Deleted nginx pod."}',
        "Deleted nginx pod."
    ])

    events2 = []
    # Instantiate recorder for the same run
    recorder2 = AgentRunRecorder(run_id=run_id, user_id=uid, session_id=sid)

    # Set run status back to running to simulate endpoint
    with db._conn() as con:
        con.execute(
            "UPDATE agent_runs SET status = 'running', error = NULL WHERE id = ?",
            (run_id,),
        )

    result2 = react_loop(
        question="delete the bad pod nginx",
        history=[],
        provider=provider2,
        dispatch_fn=fake_dispatch,
        on_event=events2.append,
        run_recorder=recorder2,
        resume_run_id=run_id,
        approved_token="token-xyz-123",
    )

    # Assert successful resume and completion
    assert result2.error is None
    assert result2.answer == "Deleted nginx pod."
    
    # Assert that the fake_dispatch was called with confirm and the correct token
    assert len(dispatched) == 4
    assert dispatched[1][0] == "delete_pod"
    assert dispatched[1][1].get("confirm") is True
    assert dispatched[1][1].get("confirmation_token") == "token-xyz-123"
    assert dispatched[2][0] == "get_pods"
    assert dispatched[3][0] == "get_events"

    # Verify database final state
    run_db = db.get_agent_run(run_id)
    assert run_db["status"] == "complete"
    assert run_db["final_answer"] == "Deleted nginx pod."

    steps_db = db.get_agent_steps(run_id)
    # The old pending step was updated to 'ok', and a new execution step and final answer step were added
    assert len(steps_db) == 3
    assert steps_db[0]["status"] == "ok"  # Approved step
    assert steps_db[1]["status"] == "ok"  # Execution step
    assert steps_db[2]["status"] == "ok"  # Answer step


def test_approve_endpoint_calls_resume(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    # Seed the user message so resume can find the question
    db.save_message(sid, "user", "delete the bad pod nginx")

    run_id = db.create_agent_run(session_id=sid, user_id=uid, route="react")
    db.suspend_agent_run(run_id)
    step_id = db.record_agent_step(
        run_id=run_id,
        iteration=1,
        action="delete_pod",
        status="pending_approval",
        params={"namespace": "default", "pod_name": "nginx"},
    )

    react_loop_called = []

    def mock_react_loop(**kwargs):
        react_loop_called.append(kwargs)
        return ReActResult(
            answer="resumed final answer",
            tool_used="delete_pod",
            result={"success": True},
            steps=[],
            total_iterations=1,
            total_duration_ms=10.0
        )

    monkeypatch.setattr("react.react_loop", mock_react_loop)

    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)

    response = client.post(
        f"/api/agent-runs/{run_id}/steps/{step_id}/approve",
        json={"token": "token-approved-123"},
    )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]

    # Read the stream content
    content = response.text
    assert "data: " in content
    assert "resumed final answer" in content

    assert len(react_loop_called) == 1
    assert react_loop_called[0]["resume_run_id"] == run_id
    assert react_loop_called[0]["approved_token"] == "token-approved-123"


def test_reject_endpoint(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    run_id = db.create_agent_run(session_id=sid, user_id=uid, route="react")
    step_id = db.record_agent_step(
        run_id=run_id,
        iteration=1,
        action="delete_pod",
        status="pending_approval",
        params={"namespace": "default", "pod_name": "nginx"},
    )

    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)

    response = client.post(
        f"/api/agent-runs/{run_id}/steps/{step_id}/reject",
        json={"reason": "don't want to delete it"},
    )

    assert response.status_code == 200
    res_json = response.json()
    assert res_json["success"] is True

    # Verify DB state
    run = db.get_agent_run(run_id)
    assert run["status"] == "aborted"
    assert run["error"] == "User rejected the operation."

    steps = db.get_agent_steps(run_id)
    assert len(steps) == 1
    assert steps[0]["status"] == "error"
    assert steps[0]["error_type"] == "approval_rejected"
    assert steps[0]["error_message"] == "User rejected the operation."


import auth

def test_approve_endpoint_no_session_authz_hole(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    
    # Alice owns the run, but session_id is None
    alice = db.create_user(username="alice", password_hash=auth.hash_password("password"))
    bob = db.create_user(username="bob", password_hash=auth.hash_password("password"))
    
    run_id = db.create_agent_run(session_id=None, user_id=alice["id"], route="react")
    db.suspend_agent_run(run_id)
    step_id = db.record_agent_step(
        run_id=run_id,
        iteration=1,
        action="delete_pod",
        status="pending_approval",
        params={"namespace": "default", "pod_name": "nginx"},
    )
    
    # Authenticate as Bob
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)
    token = auth.new_token()
    db.create_auth_session(user_id=bob["id"], token_hash=auth.token_hash(token), ttl_days=1)
    client.cookies.set("k8s_devops_auth", token)
    
    # Bob tries to approve Alice's run -> 404
    response = client.post(
        f"/api/agent-runs/{run_id}/steps/{step_id}/approve",
        json={"token": "some-token"},
    )
    assert response.status_code == 404


def test_approve_endpoint_session_present_but_different_owner_returns_404(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    
    # Alice owns session and run, bob wants to approve
    alice = db.create_user(username="alice", password_hash=auth.hash_password("password"))
    bob = db.create_user(username="bob", password_hash=auth.hash_password("password"))
    
    db.upsert_session("session-alice", user_id=alice["id"])
    run_id = db.create_agent_run(session_id="session-alice", user_id=alice["id"], route="react")
    db.suspend_agent_run(run_id)
    step_id = db.record_agent_step(
        run_id=run_id,
        iteration=1,
        action="delete_pod",
        status="pending_approval",
        params={"namespace": "default", "pod_name": "nginx"},
    )
    
    # Authenticate as Bob
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)
    token = auth.new_token()
    db.create_auth_session(user_id=bob["id"], token_hash=auth.token_hash(token), ttl_days=1)
    client.cookies.set("k8s_devops_auth", token)
    
    # Bob tries to approve Alice's run -> 404
    response = client.post(
        f"/api/agent-runs/{run_id}/steps/{step_id}/approve",
        json={"token": "some-token"},
    )
    assert response.status_code == 404


def test_approve_endpoint_concurrent_click_returns_409(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()
    
    run_id = db.create_agent_run(session_id=sid, user_id=uid, route="react")
    # Mark it already running (not suspended)
    with db._conn() as con:
        con.execute("UPDATE agent_runs SET status = 'running' WHERE id = ?", (run_id,))
        
    step_id = db.record_agent_step(
        run_id=run_id,
        iteration=1,
        action="delete_pod",
        status="pending_approval",
        params={"namespace": "default", "pod_name": "nginx"},
    )
    
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)
    
    # Try to approve already running run -> 409
    response = client.post(
        f"/api/agent-runs/{run_id}/steps/{step_id}/approve",
        json={"token": "some-token"},
    )
    assert response.status_code == 409
    assert "already running" in response.text


def test_approve_endpoint_stale_run_returns_410(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()
    
    run_id = db.create_agent_run(session_id=sid, user_id=uid, route="react")
    db.suspend_agent_run(run_id)
    # Backdate started_at by 8 days
    with db._conn() as con:
        con.execute(
            "UPDATE agent_runs SET started_at = datetime('now', '-8 days') WHERE id = ?",
            (run_id,),
        )
        
    step_id = db.record_agent_step(
        run_id=run_id,
        iteration=1,
        action="delete_pod",
        status="pending_approval",
        params={"namespace": "default", "pod_name": "nginx"},
    )
    
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)
    
    response = client.post(
        f"/api/agent-runs/{run_id}/steps/{step_id}/approve",
        json={"token": "some-token"},
    )
    assert response.status_code == 410
    assert "stale" in response.text


def test_approve_endpoint_admin_can_approve_other_users_run_and_audit_is_recorded(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    
    # Alice owns run, Charlie is admin
    alice = db.create_user(username="alice", password_hash=auth.hash_password("password"))
    charlie = db.create_user(username="charlie", password_hash=auth.hash_password("password"), role="admin")
    
    db.upsert_session("session-alice", user_id=alice["id"])
    db.save_message("session-alice", "user", "delete nginx")
    
    run_id = db.create_agent_run(session_id="session-alice", user_id=alice["id"], route="react")
    db.suspend_agent_run(run_id)
    step_id = db.record_agent_step(
        run_id=run_id,
        iteration=1,
        action="delete_pod",
        status="pending_approval",
        params={"namespace": "default", "pod_name": "nginx"},
    )
    
    # Mock react_loop to avoid executing real LLM call
    react_loop_called = []
    def mock_react_loop(**kwargs):
        react_loop_called.append(kwargs)
        # Simulate db.approve_agent_step call inside react_loop
        db.approve_agent_step(kwargs["resume_run_id"], step_id, approver_user_id=kwargs["approver_user_id"])
        return ReActResult(
            answer="admin override approved",
            tool_used="delete_pod",
            result={"success": True},
            steps=[],
            total_iterations=1,
            total_duration_ms=10.0
        )
    monkeypatch.setattr("react.react_loop", mock_react_loop)
    
    # Authenticate as Charlie
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)
    token = auth.new_token()
    db.create_auth_session(user_id=charlie["id"], token_hash=auth.token_hash(token), ttl_days=1)
    client.cookies.set("k8s_devops_auth", token)
    
    # Admin Charlie approves Alice's run -> 200
    # Also capture warning logs
    import logging
    warnings = []
    class WarningCaptureHandler(logging.Handler):
        def emit(self, record):
            if record.levelname == "WARNING":
                warnings.append(record.getMessage())
    
    from routers.chat import logger as chat_logger
    handler = WarningCaptureHandler()
    chat_logger.addHandler(handler)
    
    try:
        response = client.post(
            f"/api/agent-runs/{run_id}/steps/{step_id}/approve",
            json={"token": "admin-token-123"},
        )
        assert response.status_code == 200
        # Make sure the response stream yields successfully
        assert "admin override approved" in response.text
    finally:
        chat_logger.removeHandler(handler)
        
    # Verify warning log was recorded
    assert any("admin_approval" in w for w in warnings)
    
    # Verify approver_user_id was persisted
    steps = db.get_agent_steps(run_id)
    assert len(steps) == 1
    assert steps[0]["approver_user_id"] == charlie["id"]
