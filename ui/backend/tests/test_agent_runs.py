"""Agent run persistence + recorder + ReAct integration tests (harness Phase 1)."""

from pathlib import Path
import sys
import time

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import db  # noqa: E402
from agent_run_recorder import AgentRunRecorder, record, finish, fail  # noqa: E402


def _init_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "agent-runs.db"))
    db.init_db()


def _seed_user_and_session(uname: str = "u1") -> tuple[str, str]:
    user = db.create_user(username=uname, password_hash="x")
    db.upsert_session("s1", user_id=user["id"])
    return user["id"], "s1"


# ── DB helper unit tests ──────────────────────────────────────────────────────

def test_create_run_lifecycle(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    rid = db.create_agent_run(
        session_id=sid, user_id=uid, route="react", model="gemini-2.5-flash",
        model_params={"temperature": 0.2},
        rag_decision={"mode": "cold"},
        tool_scope=["get_pods", "investigate_pod"],
    )
    assert rid

    run = db.get_agent_run(rid)
    assert run["status"] == "running"
    assert run["model"] == "gemini-2.5-flash"
    assert run["rag_decision_json"] == {"mode": "cold"}
    assert run["tool_scope_json"] == ["get_pods", "investigate_pod"]

    db.finish_agent_run(rid, final_answer="all good", final_tool="get_pods",
                        total_tokens_in=120, total_tokens_out=70)
    done = db.get_agent_run(rid)
    assert done["status"] == "complete"
    assert done["final_answer"] == "all good"
    assert done["total_tokens_in"] == 120
    assert done["ended_at"] is not None


def test_fail_run_marks_aborted(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()
    rid = db.create_agent_run(session_id=sid, user_id=uid, route="react")

    db.fail_agent_run(rid, error="wall clock timeout", status="aborted")
    run = db.get_agent_run(rid)
    assert run["status"] == "aborted"
    assert run["error"] == "wall clock timeout"

    # status outside the allowlist gets normalized to 'failed'
    rid2 = db.create_agent_run(session_id=sid, user_id=uid, route="react")
    db.fail_agent_run(rid2, error="weird", status="garbage")
    assert db.get_agent_run(rid2)["status"] == "failed"


def test_record_steps_and_get_back(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()
    rid = db.create_agent_run(session_id=sid, user_id=uid, route="react")

    db.record_agent_step(
        run_id=rid, iteration=1, action="get_pods", status="ok",
        thought="list pods first", params={"namespace": "default"},
        observation_preview="3 pods running", duration_ms=85,
    )
    db.record_agent_step(
        run_id=rid, iteration=2, action="investigate_pod", status="error",
        thought="probe failing", params={"pod": "nginx-1"},
        error_type="invalid_params", error_message="pod not found",
        duration_ms=42,
    )
    db.record_agent_step(
        run_id=rid, iteration=3, action="answer", status="ok",
        step_kind="answer", thought="we have enough",
    )

    steps = db.get_agent_steps(rid)
    assert len(steps) == 3
    assert [s["action"] for s in steps] == ["get_pods", "investigate_pod", "answer"]
    assert steps[0]["params_json"] == {"namespace": "default"}
    assert steps[1]["status"] == "error"
    assert steps[1]["error_type"] == "invalid_params"
    assert steps[2]["step_kind"] == "answer"


def test_list_runs_is_user_scoped(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    u1_id, _ = _seed_user_and_session("u1")
    u2 = db.create_user(username="u2", password_hash="x")
    db.upsert_session("s2", user_id=u2["id"])

    db.create_agent_run(session_id="s1", user_id=u1_id, route="react")
    db.create_agent_run(session_id="s1", user_id=u1_id, route="react")
    db.create_agent_run(session_id="s2", user_id=u2["id"], route="react")

    assert len(db.list_agent_runs(user_id=u1_id)) == 2
    assert len(db.list_agent_runs(user_id=u2["id"])) == 1
    assert len(db.list_agent_runs(session_id="s2")) == 1
    # No filter returns all
    assert len(db.list_agent_runs()) == 3


def test_prune_respects_retention_policy(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    # Two runs: standard + curated
    rid_std = db.create_agent_run(session_id=sid, user_id=uid, route="react",
                                  retention_policy="standard")
    rid_keep = db.create_agent_run(session_id=sid, user_id=uid, route="react",
                                   retention_policy="golden")

    # Backdate both to before the cutoff
    import sqlite3
    with sqlite3.connect(db.DB_PATH) as con:
        con.execute("UPDATE agent_runs SET started_at = datetime('now', '-30 days') WHERE id IN (?, ?)",
                    (rid_std, rid_keep))
        con.commit()

    deleted = db.prune_agent_runs(retention_days=7)
    assert deleted == 1
    assert db.get_agent_run(rid_std) is None
    assert db.get_agent_run(rid_keep) is not None  # golden survives


# ── Recorder facade tests ─────────────────────────────────────────────────────

def test_recorder_redacts_params_and_observation(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    r = AgentRunRecorder.start(session_id=sid, user_id=uid, route="react",
                               model="gemini-2.5-flash")
    assert r is not None

    record(r, iteration=1, action="get_pods", status="ok",
           thought="planning",
           params={"namespace": "default", "token": "shh-secret",
                   "auth": "bearer xyz"},
           observation_preview={"pods": ["nginx-1"]},
           duration_ms=85)
    finish(r, final_answer="done", final_tool="get_pods")

    steps = db.get_agent_steps(r.run_id)
    assert steps[0]["params_json"]["namespace"] == "default"
    assert steps[0]["params_json"]["token"] == "<REDACTED>"
    assert steps[0]["params_json"]["auth"] == "<REDACTED>"
    assert "nginx-1" in steps[0]["observation_preview"]


def test_recorder_recurses_into_lists_of_dicts(monkeypatch, tmp_path):
    """Bug fix: key-based redaction must reach dicts nested inside a list."""
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    r = AgentRunRecorder.start(session_id=sid, user_id=uid, route="react")
    record(r, iteration=1, action="kubectl_apply", status="ok",
           params={
               # Flat list (strings) — non-secret keys, passes through.
               "args": ["--namespace=prod", "--server=https://x"],
               # List of dicts — each dict gets key-based redaction.
               "credentials": [
                   {"name": "alice", "token": "shh-secret"},
                   {"name": "bob",   "password": "hunter2"},
               ],
           })
    finish(r, final_answer="done")

    steps = db.get_agent_steps(r.run_id)
    redacted = steps[0]["params_json"]
    assert redacted["args"] == ["--namespace=prod", "--server=https://x"]
    # The dicts INSIDE the list had their secret-shaped keys redacted.
    assert redacted["credentials"][0]["token"] == "<REDACTED>"
    assert redacted["credentials"][1]["password"] == "<REDACTED>"
    # Non-secret fields preserved.
    assert redacted["credentials"][0]["name"] == "alice"
    assert redacted["credentials"][1]["name"] == "bob"


def test_recorder_none_safe_passthroughs():
    """The module-level helpers must accept None without raising."""
    record(None, iteration=1, action="ignored", status="ok")
    finish(None, final_answer="anything")
    fail(None, error="anything")


def test_recorder_swallows_db_errors(monkeypatch, tmp_path):
    """A persistence failure must not propagate. The chat flow must keep working."""
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    r = AgentRunRecorder.start(session_id=sid, user_id=uid, route="react")
    assert r is not None

    # Force the DB helper to raise
    def boom(*args, **kwargs):
        raise RuntimeError("simulated DB outage")
    monkeypatch.setattr(db, "record_agent_step", boom)
    monkeypatch.setattr(db, "finish_agent_run", boom)

    # These must not raise
    record(r, iteration=1, action="get_pods", status="ok")
    finish(r, final_answer="done")


def test_recorder_start_returns_none_on_db_error(monkeypatch, tmp_path):
    """If opening the run fails entirely, start() returns None and callers skip recording."""
    _init_temp_db(monkeypatch, tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("FK violation or similar")
    monkeypatch.setattr(db, "create_agent_run", boom)

    r = AgentRunRecorder.start(session_id="s1", user_id="u1", route="react")
    assert r is None


# ── ReAct integration test ───────────────────────────────────────────────────

class _FakeProvider:
    """Minimal provider that emits scripted ReAct JSON responses."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def generate_stream(self, prompt, system=None, temperature=0.1, max_tokens=8000):
        self.calls += 1
        idx = min(self.calls - 1, len(self._responses) - 1)
        yield self._responses[idx]

    def generate(self, prompt, system=None, temperature=0.2, max_tokens=8000):
        return ""


def test_react_loop_records_full_trace(monkeypatch, tmp_path):
    """End-to-end: a successful react_loop run writes one agent_runs row + per-step rows."""
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    from react import react_loop

    # Script: iter 1 calls get_pods, iter 2 answers
    provider = _FakeProvider([
        '{"thought": "list pods", "action": "get_pods", "params": {"namespace": "default"}}',
        '{"thought": "have data", "action": "answer", "answer": "There are 2 pods."}',
    ])

    def dispatch(action, params):
        if action == "get_pods":
            return {"pods": [{"name": "nginx-1"}, {"name": "nginx-2"}]}
        return {"error": "unknown_tool"}

    recorder = AgentRunRecorder.start(session_id=sid, user_id=uid, route="react",
                                      model="fake")
    assert recorder is not None

    result = react_loop(
        question="how many pods?",
        history=[],
        provider=provider,
        dispatch_fn=dispatch,
        max_iterations=4,
        run_recorder=recorder,
    )

    run = db.get_agent_run(recorder.run_id)
    steps = db.get_agent_steps(recorder.run_id)

    assert run["status"] == "complete"
    assert run["user_id"] == uid
    assert run["session_id"] == sid
    # Step trace: one tool step (get_pods) + one answer step
    actions = [s["action"] for s in steps]
    assert "get_pods" in actions
    assert "answer" in actions
    # The tool step records the obs preview
    tool_step = next(s for s in steps if s["action"] == "get_pods")
    assert "nginx-1" in tool_step["observation_preview"]
    assert tool_step["params_json"] == {"namespace": "default"}
    # The result returned to the caller still works
    assert result.tool_used == "get_pods"


def test_react_loop_records_known_vs_exception_error_types(monkeypatch, tmp_path):
    """error_type stays a category for known codes and 'exception' for raw exception messages."""
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    from react import react_loop

    # Iter 1: dispatch raises -> result.error = str(exc), error_type should be 'exception'
    # Iter 2: dispatch returns a known error code
    # Iter 3: answer
    provider = _FakeProvider([
        '{"thought": "try x", "action": "get_pods", "params": {"namespace": "default"}}',
        '{"thought": "try unknown", "action": "totally_unknown_tool", "params": {}}',
        '{"thought": "give up", "action": "answer", "answer": "nope"}',
    ])

    def dispatch(action, params):
        if action == "get_pods":
            raise RuntimeError("boom from dispatch")
        if action == "totally_unknown_tool":
            return {"error": "unknown_tool", "message": "tool not registered"}
        return {"error": "unknown_tool"}

    recorder = AgentRunRecorder.start(session_id=sid, user_id=uid, route="react", model="fake")
    react_loop(
        question="?", history=[], provider=provider, dispatch_fn=dispatch,
        max_iterations=5, run_recorder=recorder,
    )

    steps = db.get_agent_steps(recorder.run_id)
    by_action = {s["action"]: s for s in steps}

    assert by_action["get_pods"]["error_type"] == "unexpected"
    assert "boom" in by_action["get_pods"]["error_message"]
    assert by_action["totally_unknown_tool"]["error_type"] == "llm_recoverable"
    assert by_action["totally_unknown_tool"]["error_message"] == "tool not registered"
