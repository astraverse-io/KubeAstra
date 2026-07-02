"""Unit tests for Phase 5 Error Handling Taxonomy and Retries."""

from pathlib import Path
import inspect
import sys
import json
import time
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import db
from agent_errors import classify_error, AgentErrorType
from react import react_loop, ReActStep, ReActResult


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
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "agent-errors-test.db"))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    db.init_db()


def _seed_user_and_session() -> tuple[str, str]:
    user = db.create_user(username="testuser", password_hash="hash")
    db.upsert_session("session-test", user_id=user["id"])
    return user["id"], "session-test"


def _import_recorder_or_skip():
    if "run_recorder" not in inspect.signature(react_loop).parameters:
        pytest.skip("react_loop recorder integration is not present on this branch")
    module = pytest.importorskip("agent_run_recorder")
    return module.AgentRunRecorder


def test_classify_error():
    # Policy Blocked
    assert classify_error("tool_out_of_scope") == AgentErrorType.POLICY_BLOCKED
    assert classify_error("some_code", "Operation was rejected by user policy") == AgentErrorType.POLICY_BLOCKED

    # LLM Recoverable
    assert classify_error("unknown_tool") == AgentErrorType.LLM_RECOVERABLE
    assert classify_error("invalid_params") == AgentErrorType.LLM_RECOVERABLE

    # Rate Limited
    assert classify_error("ResourceExhausted", "API quota limit exceeded (429)") == AgentErrorType.RATE_LIMITED

    # Transient
    assert classify_error("timeout", "503 Service Unavailable") == AgentErrorType.TRANSIENT
    assert classify_error("some_code", "connection refused by target host") == AgentErrorType.TRANSIENT

    # User Fixable
    assert classify_error("missing namespace") == AgentErrorType.USER_FIXABLE
    assert classify_error("ssh_auth_failed", "unauthorized access to master node") == AgentErrorType.USER_FIXABLE

    # Unexpected
    assert classify_error("InternalError", "something strange happened") == AgentErrorType.UNEXPECTED


def test_react_loop_recorder_records_llm_recoverable_retry(monkeypatch, tmp_path):
    AgentRunRecorder = _import_recorder_or_skip()
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    provider = SequencedProvider([
        '{"thought":"I will check the pods","action":"get_pods","params":{"namespace":"default"}}',
        '{"thought":"The params failed, so I will retry with corrected params","action":"get_pods","params":{"namespace":"kube-system"}}',
        '{"thought":"Pods check completed, so I can answer.","action":"answer","answer":"Retried and succeeded."}',
        "Retried and succeeded."
    ])

    dispatched = []

    def fake_dispatch(tool, params):
        dispatched.append((tool, params.copy()))
        if len(dispatched) == 1:
            # First attempt fails with an LLM-recoverable tool-call error.
            return {"error": "invalid_params", "message": "missing namespace"}
        # Second attempt succeeds
        return {"success": True, "pods": []}

    recorder = AgentRunRecorder.start(
        session_id=sid,
        user_id=uid,
        route="react",
        model="gemini-fake",
    )
    run_id = recorder.run_id

    result = react_loop(
        question="check my pods",
        history=[],
        provider=provider,
        dispatch_fn=fake_dispatch,
        run_recorder=recorder,
    )

    assert result.error is None
    assert "Retried and succeeded." in result.answer

    # Assert that the tool was dispatched twice (the first failed, then retried)
    assert len(dispatched) == 2
    assert dispatched[0][0] == "get_pods"
    assert dispatched[1][0] == "get_pods"

    # Assert that the steps were recorded correctly. The harness records each
    # iteration separately, so we see the failed attempt, the successful retry,
    # and the final answer step.
    steps_db = db.get_agent_steps(run_id)
    assert len(steps_db) == 3
    assert steps_db[0]["action"] == "get_pods"
    assert steps_db[0]["status"] == "error"
    assert steps_db[1]["action"] == "get_pods"
    assert steps_db[1]["status"] == "ok"
    assert steps_db[2]["action"] == "answer"


def test_react_loop_user_fixable_no_retry(monkeypatch, tmp_path):
    AgentRunRecorder = _import_recorder_or_skip()
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    provider = SequencedProvider([
        '{"thought":"I will check the pods","action":"get_pods","params":{"namespace":"default"}}',
        '{"thought":"Failed with missing namespace, so I can answer.","action":"answer","answer":"Error: missing namespace."}',
        "Error: missing namespace."
    ])

    dispatched = []

    def fake_dispatch(tool, params):
        dispatched.append((tool, params.copy()))
        # Fails with user_fixable error (should NOT retry)
        return {"error": "validation_error", "message": "missing namespace specifier"}

    recorder = AgentRunRecorder.start(
        session_id=sid,
        user_id=uid,
        route="react",
        model="gemini-fake",
    )
    run_id = recorder.run_id

    result = react_loop(
        question="check my pods",
        history=[],
        provider=provider,
        dispatch_fn=fake_dispatch,
        run_recorder=recorder,
    )

    # Dispatched exactly once
    assert len(dispatched) == 1

    steps_db = db.get_agent_steps(run_id)
    assert len(steps_db) == 2
    assert steps_db[0]["action"] == "get_pods"
    assert steps_db[0]["status"] == "error"
    # It should have the classified error type stored
    assert steps_db[0]["error_type"] == AgentErrorType.USER_FIXABLE.value


def test_react_loop_retries_llm_recoverable_tool_error_without_recorder():
    provider = SequencedProvider([
        '{"thought":"I will check pods","action":"get_pods","params":{}}',
        '{"thought":"I will retry with the required namespace","action":"get_pods","params":{"namespace":"default"}}',
        '{"thought":"Pods check completed, so I can answer.","action":"answer","answer":"Retried and succeeded."}',
        "Retried and succeeded."
    ])

    dispatched = []

    def fake_dispatch(tool, params):
        dispatched.append((tool, params.copy()))
        if len(dispatched) == 1:
            return {
                "error": "invalid_params",
                "message": "missing namespace",
                "expected_schema": {"namespace": "string"},
            }
        return {"success": True, "pods": []}

    result = react_loop(
        question="check my pods",
        history=[],
        provider=provider,
        dispatch_fn=fake_dispatch,
    )

    assert result.error is None
    assert "Retried and succeeded." in result.answer
    assert len(dispatched) == 2
    assert dispatched[0] == ("get_pods", {})
    assert dispatched[1] == ("get_pods", {"namespace": "default"})


def test_react_loop_user_fixable_error_does_not_retry_without_recorder():
    provider = SequencedProvider([
        '{"thought":"I will check pods","action":"get_pods","params":{"namespace":"default"}}',
        '{"thought":"The request is missing a namespace, so I can answer.","action":"answer","answer":"Error: missing namespace."}',
        "Error: missing namespace."
    ])

    dispatched = []

    def fake_dispatch(tool, params):
        dispatched.append((tool, params.copy()))
        return {"error": "validation_error", "message": "missing namespace specifier"}

    result = react_loop(
        question="check my pods",
        history=[],
        provider=provider,
        dispatch_fn=fake_dispatch,
    )

    assert result.error is None
    assert "missing namespace" in result.answer
    assert len(dispatched) == 1
