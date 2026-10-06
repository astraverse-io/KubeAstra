"""ReAct wiring for proposed writes (Phase 2, PR 5).

propose_file_edit parks a pending write; the loop must surface it to the UI as
one `write_proposed` event and then KEEP GOING (spec D2) — unlike a folder
grant, the agent doesn't need the outcome to finish its answer. The approval
itself happens later, through POST /api/desktop/files/apply.
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


PENDING = {
    "success": True,
    "pending_write": {
        "token": "pwr_abc123", "path": "prod/api.yaml", "root": "/infra", "created": False,
        "reason": "raise replicas", "diff": "-  replicas: 2\n+  replicas: 4\n",
        "validation": {"ok": True, "validated": False,
                       "unvalidated_reason": "kubeconform: kubeconform not installed",
                       "checks": [{"name": "yaml_parse", "status": "pass", "detail": ""}]},
        "expires_at": 1.0,
    },
    "message": "Edit proposed and validated. It is NOT written yet.",
}


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "writes-react-test.db"))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    db.init_db()
    user = db.create_user(username="testuser", password_hash="hash")
    db.upsert_session("session-test", user_id=user["id"])
    db.save_message("session-test", "user", "api is under-provisioned, fix it")
    return user["id"], "session-test"


def _run(monkeypatch, tmp_path, dispatch):
    uid, sid = _setup(monkeypatch, tmp_path)
    provider = SequencedProvider([
        '{"thought":"raise replicas in the manifest","action":"propose_file_edit",'
        '"params":{"path":"/infra/prod/api.yaml","reason":"raise replicas",'
        '"edits":[{"old":"replicas: 2","new":"replicas: 4"}]}}',
        '{"thought":"proposed; tell the user","action":"answer",'
        '"answer":"I proposed raising replicas to 4 — approve the edit to write it."}',
        "I proposed raising replicas to 4 — approve the edit to write it.",
    ])
    events = []
    rec = AgentRunRecorder.start(session_id=sid, user_id=uid, route="react", model="fake")
    result = react_loop(
        question="api is under-provisioned, fix it", history=[], provider=provider,
        dispatch_fn=dispatch, on_event=events.append, run_recorder=rec,
    )
    return result, events, rec.run_id


def test_pending_write_emits_one_event_and_the_run_continues(monkeypatch, tmp_path):
    dispatched = []

    def dispatch(tool, params):
        dispatched.append(tool)
        return PENDING

    result, events, run_id = _run(monkeypatch, tmp_path, dispatch)

    proposed = [e for e in events if e.get("type") == "write_proposed"]
    assert len(proposed) == 1
    e = proposed[0]
    assert e["token"] == "pwr_abc123" and e["path"] == "prod/api.yaml"
    assert e["created"] is False and e["reason"] == "raise replicas"
    assert e["validation"]["validated"] is False
    assert "+  replicas: 4" in e["diff"]

    # Not suspended: the loop went on to answer.
    assert result.error is None
    assert "approve" in result.answer
    assert dispatched == ["propose_file_edit"]
    assert db.get_agent_run(run_id)["status"] != "suspended"
    assert not any(e.get("type") in ("access_required", "approval_required") for e in events)


def test_envelope_payload_is_recognised(monkeypatch, tmp_path):
    # On the real react surface the raw result rides in `payload` of an envelope.
    wrapped = {"summary": "…", "payload": PENDING}
    result, events, _ = _run(monkeypatch, tmp_path, lambda tool, params: wrapped)
    assert len([e for e in events if e.get("type") == "write_proposed"]) == 1


def test_invalid_proposal_emits_nothing(monkeypatch, tmp_path):
    invalid = {"success": False, "invalid": True, "path": "prod/api.yaml",
               "failures": [{"name": "policy", "status": "fail", "detail": "privileged"}],
               "attempts_remaining": 2}
    result, events, _ = _run(monkeypatch, tmp_path, lambda tool, params: invalid)
    assert not any(e.get("type") == "write_proposed" for e in events)
