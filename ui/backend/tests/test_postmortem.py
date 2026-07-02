"""Unit and integration tests for the Postmortem Writer subagent."""

from pathlib import Path
import sys
import os
import json
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import db
import postmortem_writer

class FakeLLMProvider:
    def __init__(self, response="Mock markdown postmortem"):
        self.response = response
        self.calls = 0
        self.enabled = True

    def generate(self, prompt, system=None, temperature=0.2):
        self.calls += 1
        return self.response


def _init_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test-postmortem.db"))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    db.init_db()


def _seed_user_and_session() -> tuple[str, str]:
    user = db.create_user(username="testuser", password_hash="hash")
    db.upsert_session("session-test", user_id=user["id"])
    return user["id"], "session-test"


def test_generate_postmortem_prompt(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()
    
    run_id = db.create_agent_run(
        session_id=sid, user_id=uid, route="react",
        memory_snapshot="fix crashlooping pod in payment namespace"
    )
    db.record_agent_step(
        run_id=run_id, iteration=1, action="get_pods", status="ok",
        thought="checking status", params={"namespace": "payment"},
        observation_preview="payment-api-123 is CrashLoopBackOff", duration_ms=45
    )
    db.record_agent_step(
        run_id=run_id, iteration=2, action="get_pod_logs", status="error",
        thought="reading logs", params={"pod": "payment-api-123", "namespace": "payment"},
        error_type="user_fixable", error_message="permission denied to read logs", duration_ms=20
    )
    db.record_agent_step(
        run_id=run_id, iteration=3, action="context_compaction", status="ok",
        step_kind="compaction", thought="compacting context",
        params={"original_len": 5000, "summary_len": 200},
        observation_preview="Compacted observation preview text", duration_ms=10
    )
    db.finish_agent_run(run_id, final_answer="The pod is crashing due to database timeout.")
    
    run = db.get_agent_run(run_id)
    steps = db.get_agent_steps(run_id)
    
    prompt = postmortem_writer.generate_postmortem_prompt(run, steps)
    
    assert "USER REQUEST: fix crashlooping pod in payment namespace" in prompt
    assert "FINAL ANSWER SUMMARY: The pod is crashing due to database timeout." in prompt
    assert "Thought: checking status" in prompt
    assert "Action: get_pods with params {'namespace': 'payment'}" in prompt
    assert "Observation: payment-api-123 is CrashLoopBackOff" in prompt
    assert "Status: ERROR (user_fixable): permission denied to read logs" in prompt
    assert "--- STEP 3 (compaction) ---" in prompt
    assert "Thought: compacting context" in prompt
    assert "Action: context_compaction with params {'original_len': 5000, 'summary_len': 200}" in prompt
    assert "Observation: Compacted observation preview text" in prompt


def test_write_postmortem_db_save(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    run_id = db.create_agent_run(session_id=sid, user_id=uid, route="react")
    db.record_agent_step(run_id=run_id, iteration=1, action="get_pods", status="ok")
    db.finish_agent_run(run_id, final_answer="done")

    provider = FakeLLMProvider("Highly detailed Markdown SRE Postmortem report")
    report = postmortem_writer.write_postmortem(run_id, provider=provider)
    
    assert report == "Highly detailed Markdown SRE Postmortem report"
    assert provider.calls == 1
    
    # Assert it was saved to database
    run_db = db.get_agent_run(run_id)
    assert run_db["postmortem"] == "Highly detailed Markdown SRE Postmortem report"


def test_postmortem_endpoint(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    run_id = db.create_agent_run(session_id=sid, user_id=uid, route="react")
    db.finish_agent_run(run_id, final_answer="success")

    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)

    provider = FakeLLMProvider("Endpoint postmortem output")

    # Mock postmortem_writer.get_llm_provider to return our provider
    with patch("postmortem_writer.get_llm_provider", return_value=provider):
        # 1. Call endpoint first time -> generates and caches
        response = client.post(f"/api/agent-runs/{run_id}/postmortem")
        assert response.status_code == 200
        assert response.json()["postmortem"] == "Endpoint postmortem output"
        assert response.json()["cached"] is False
        assert provider.calls == 1

        # 2. Call endpoint second time -> returns cached
        response = client.post(f"/api/agent-runs/{run_id}/postmortem")
        assert response.status_code == 200
        assert response.json()["postmortem"] == "Endpoint postmortem output"
        assert response.json()["cached"] is True
        assert provider.calls == 1  # Should not increase

        # 3. Call endpoint with force=True -> regenerates
        response = client.post(f"/api/agent-runs/{run_id}/postmortem?force=true")
        assert response.status_code == 200
        assert response.json()["cached"] is False
        assert provider.calls == 2


def test_postmortem_fallback_to_session_history(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()
    
    # Save messages in history
    db.save_message(sid, "user", "first oldest user message")
    db.save_message(sid, "assistant", "some response")
    db.save_message(sid, "user", "second user message")
    
    # Run has empty memory_snapshot
    run_id = db.create_agent_run(session_id=sid, user_id=uid, route="react", memory_snapshot=None)
    db.record_agent_step(run_id=run_id, iteration=1, action="get_pods", status="ok")
    db.finish_agent_run(run_id, final_answer="done")
    
    run = db.get_agent_run(run_id)
    steps = db.get_agent_steps(run_id)
    
    prompt = postmortem_writer.generate_postmortem_prompt(run, steps)
    # Should fallback to the oldest user message
    assert "USER REQUEST: first oldest user message" in prompt
