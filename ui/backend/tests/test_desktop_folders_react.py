"""ReAct consent pause/resume for local-folder tools (Phase 1, PR 6).

A folder tool returning {needs_access: …} must suspend the run with a
pending_folder_grant step and an access_required event (mirroring the mutating
approval flow), and a resume — WITHOUT a token, since the grant itself is the
durable approval — must re-run the same tool call and complete.
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for p in (BACKEND_DIR, MCP_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import db  # noqa: E402
from react import react_loop  # noqa: E402
from agent_run_recorder import AgentRunRecorder  # noqa: E402


class SequencedProvider:
    def __init__(self, responses):
        self.responses = list(responses)

    def generate_stream(self, prompt, system=None, temperature=0.1, max_tokens=8000):
        assert self.responses, "provider called more times than expected"
        for char in self.responses.pop(0):
            yield char


def _init_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "folder-react-test.db"))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    db.init_db()


def _seed():
    user = db.create_user(username="testuser", password_hash="hash")
    db.upsert_session("session-test", user_id=user["id"])
    return user["id"], "session-test"


def test_folder_tool_suspends_on_needs_access(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed()
    db.save_message(sid, "user", "read the api manifest")

    provider = SequencedProvider([
        '{"thought":"I need the manifest","action":"read_file",'
        '"params":{"path":"/infra/api.yaml","reason":"inspect api-gateway"}}',
    ])
    dispatched = []

    def fake_dispatch(tool, params):
        dispatched.append((tool, params.copy()))
        return {"needs_access": {"path": "/infra/api.yaml", "mode": "read",
                                 "reason": "inspect api-gateway"}}

    events = []
    rec = AgentRunRecorder.start(session_id=sid, user_id=uid, route="react", model="fake")
    run_id = rec.run_id

    result = react_loop(
        question="read the api manifest", history=[], provider=provider,
        dispatch_fn=fake_dispatch, on_event=events.append, run_recorder=rec,
    )

    assert result.error == "PendingFolderGrant"
    assert "/infra/api.yaml" in result.answer
    assert dispatched == [("read_file", {"path": "/infra/api.yaml", "reason": "inspect api-gateway"})]

    assert db.get_agent_run(run_id)["status"] == "suspended"
    step = db.get_agent_steps(run_id)[-1]
    assert step["status"] == "pending_folder_grant"
    assert step["action"] == "read_file"

    access = [e for e in events if e.get("type") == "access_required"]
    assert len(access) == 1
    assert access[0]["path"] == "/infra/api.yaml"
    assert access[0]["mode"] == "read"
    assert access[0]["reason"] == "inspect api-gateway"
    assert access[0]["run_id"] == run_id
    assert access[0]["step_id"] == step["id"]


def test_folder_grant_resume_reruns_tool_without_token(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed()
    db.save_message(sid, "user", "read the api manifest")

    # A run already suspended awaiting a folder grant.
    run_id = db.create_agent_run(session_id=sid, user_id=uid, route="react")
    db.suspend_agent_run(run_id)
    db.record_agent_step(
        run_id=run_id, iteration=1, action="read_file",
        status="pending_folder_grant",
        params={"path": "/infra/api.yaml", "reason": "inspect api-gateway"},
    )

    dispatched = []

    def fake_dispatch(tool, params):
        dispatched.append((tool, params.copy()))
        if tool == "read_file":
            return {"success": True, "content": "kind: Deployment\nmetadata:\n  name: api-gateway\n"}
        return {"success": True}

    # After the retry runs read_file, the agent answers.
    provider = SequencedProvider([
        '{"thought":"I have the file now","action":"answer",'
        '"answer":"The manifest defines a Deployment named api-gateway."}',
        "The manifest defines a Deployment named api-gateway.",
    ])
    rec = AgentRunRecorder(run_id=run_id, user_id=uid, session_id=sid)
    with db._conn() as con:
        con.execute("UPDATE agent_runs SET status='running', error=NULL WHERE id=?", (run_id,))

    result = react_loop(
        question="read the api manifest", history=[], provider=provider,
        dispatch_fn=fake_dispatch, on_event=lambda e: None, run_recorder=rec,
        resume_run_id=run_id,   # NO approved_token — the grant is the approval
    )

    assert result.error is None
    # the suspended tool call was retried with its original params
    assert dispatched[0] == ("read_file", {"path": "/infra/api.yaml", "reason": "inspect api-gateway"})
    assert "api-gateway" in result.answer
    assert db.get_agent_run(run_id)["status"] == "complete"
