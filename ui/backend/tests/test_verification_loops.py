"""Unit tests for Phase 4 Verification Loops."""

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
from react import react_loop, ReActStep, ReActResult, run_verification_sub_run


class SequencedProvider:
    def __init__(self, responses):
        self.responses = list(responses)

    def generate(self, prompt, system=None, temperature=0.1, max_tokens=8000):
        assert self.responses, "provider called more times than expected"
        return self.responses.pop(0)

    def generate_stream(self, prompt, system=None, temperature=0.1, max_tokens=8000):
        assert self.responses, "provider called more times than expected"
        response = self.responses.pop(0)
        for char in response:
            yield char


def _init_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "verification-loops-test.db"))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    db.init_db()


def _seed_user_and_session() -> tuple[str, str]:
    user = db.create_user(username="testuser", password_hash="hash")
    db.upsert_session("session-test", user_id=user["id"])
    return user["id"], "session-test"


def test_verification_loop_delete_pod(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    provider = SequencedProvider([
        # Response 1: LLM decides to call delete_pod with confirm=true
        '{"thought":"I need to delete pod nginx to fix it","action":"delete_pod","params":{"namespace":"default","pod_name":"nginx","confirm":true}}',
        # Response 2: LLM call inside verification sub-run (verifying delete_pod success)
        "Verification verdict: SUCCESS. nginx pod was successfully deleted.",
        # Response 3: LLM final synthesis response in main ReAct loop
        '{"thought":"Verification report confirms deletion, so I can answer.","action":"answer","answer":"Deleted nginx pod successfully."}',
        "Deleted nginx pod successfully."
    ])

    dispatched = []

    def fake_dispatch(tool, params):
        dispatched.append((tool, params.copy()))
        if tool == "delete_pod":
            return {"success": True, "message": "pod nginx deleted"}
        elif tool == "get_pods":
            return {"success": True, "pods": []}
        elif tool == "get_events":
            return {"success": True, "events": []}
        return {"error": f"Unknown tool: {tool}"}

    from agent_run_recorder import AgentRunRecorder
    recorder = AgentRunRecorder.start(
        session_id=sid,
        user_id=uid,
        route="react",
        model="gemini-fake",
    )
    parent_run_id = recorder.run_id

    result = react_loop(
        question="delete pod nginx",
        history=[],
        provider=provider,
        dispatch_fn=fake_dispatch,
        run_recorder=recorder,
    )

    assert result.error is None
    assert "Deleted nginx pod successfully" in result.answer

    # Assert correct tools were dispatched
    assert len(dispatched) == 3
    assert dispatched[0][0] == "delete_pod"
    assert dispatched[0][1].get("confirm") is True
    assert dispatched[1][0] == "get_pods"
    assert dispatched[1][1] == {"namespace": "default"}
    assert dispatched[2][0] == "get_events"
    assert dispatched[2][1] == {"namespace": "default"}

    # Assert sub-run database state
    with db._conn() as con:
        runs = [dict(r) for r in con.execute("SELECT * FROM agent_runs").fetchall()]

    assert len(runs) == 2
    parent_row = [r for r in runs if r["id"] == parent_run_id][0]
    child_row = [r for r in runs if r["parent_run_id"] == parent_run_id][0]
    assert child_row["route"] == "verification"
    assert child_row["parent_run_id"] == parent_run_id
    assert child_row["status"] == "complete"
    assert "Verification verdict: SUCCESS" in child_row["final_answer"]

    # Verify that sub-run steps are also logged in agent_steps table
    with db._conn() as con:
        sub_steps = [dict(s) for s in con.execute("SELECT * FROM agent_steps WHERE run_id = ?", (child_row["id"],)).fetchall()]
    assert len(sub_steps) == 2
    assert sub_steps[0]["action"] == "get_pods"
    assert sub_steps[0]["status"] == "ok"
    assert sub_steps[1]["action"] == "get_events"
    assert sub_steps[1]["status"] == "ok"


def test_verification_loop_rollout_restart(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    provider = SequencedProvider([
        '{"thought":"I need to rollout restart","action":"rollout_restart","params":{"namespace":"web","deployment_name":"frontend","confirm":true}}',
        "Verification verdict: SUCCESS. Rollout restart succeeded.",
        '{"thought":"Finished","action":"answer","answer":"Restarted deployment frontend."}',
        "Restarted deployment frontend."
    ])

    dispatched = []

    def fake_dispatch(tool, params):
        dispatched.append((tool, params.copy()))
        if tool == "rollout_restart":
            return {"success": True}
        elif tool == "get_rollout_status":
            return {"success": True, "status": "successfully rolled out"}
        elif tool == "get_pods":
            return {"success": True, "pods": []}
        elif tool == "get_events":
            return {"success": True, "events": []}
        return {"error": f"Unknown tool: {tool}"}

    from agent_run_recorder import AgentRunRecorder
    recorder = AgentRunRecorder.start(
        session_id=sid,
        user_id=uid,
        route="react",
        model="gemini-fake",
    )
    parent_run_id = recorder.run_id

    result = react_loop(
        question="restart web app",
        history=[],
        provider=provider,
        dispatch_fn=fake_dispatch,
        run_recorder=recorder,
    )

    assert result.error is None
    assert len(dispatched) == 4
    assert dispatched[0][0] == "rollout_restart"
    assert dispatched[1][0] == "get_rollout_status"
    assert dispatched[1][1] == {"namespace": "web", "deployment_name": "frontend"}
    assert dispatched[2][0] == "get_pods"
    assert dispatched[2][1] == {"namespace": "web"}
    assert dispatched[3][0] == "get_events"
    assert dispatched[3][1] == {"namespace": "web"}

    with db._conn() as con:
        runs = [dict(r) for r in con.execute("SELECT * FROM agent_runs").fetchall()]
    assert len(runs) == 2
    child_row = [r for r in runs if r["parent_run_id"] == parent_run_id][0]
    assert child_row["route"] == "verification"
