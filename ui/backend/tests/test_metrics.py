"""Unit and integration tests for Prometheus metrics definitions and telemetry tracking (Phase 2)."""

from pathlib import Path
import sys
import os
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from main import app
import db
import react
from services.llm.pricing import TokenUsage
from services.synthesis_critic import run_synthesis_critic


# Register a simulated 500 error route to test server_error metrics bucketing
@app.get("/api/test-error-500")
def trigger_error_500():
    raise HTTPException(status_code=500, detail="Simulated 500 error")


class CostEmittingProvider:
    """Fake provider that returns canned text plus deterministic usage."""

    name = "fake"
    model = "gemini-3.1-flash-lite"
    enabled = True

    def __init__(self, scripted_responses: list[str], usage_per_call: TokenUsage):
        self._responses = list(scripted_responses)
        self._usage = usage_per_call

    def _next_response(self) -> str:
        assert self._responses, "provider called more times than scripted"
        return self._responses.pop(0)

    def generate(self, prompt, system=None, temperature=0.2, max_tokens=None):
        return self._next_response()

    def generate_with_usage(self, prompt, system=None, temperature=0.2, max_tokens=None):
        return self._next_response(), TokenUsage(
            tokens_in=self._usage.tokens_in,
            cached_tokens_in=self._usage.cached_tokens_in,
            tokens_out=self._usage.tokens_out,
            cost_usd=self._usage.cost_usd,
            model=self.model,
        )

    def generate_stream(self, prompt, system=None, temperature=0.2, max_tokens=None):
        response = self._next_response()
        for ch in response:
            yield ch

    def generate_stream_with_usage(self, prompt, system=None, temperature=0.2, max_tokens=None):
        usage_holder: list[TokenUsage] = []
        response = self._next_response()
        per_call_usage = TokenUsage(
            tokens_in=self._usage.tokens_in,
            cached_tokens_in=self._usage.cached_tokens_in,
            tokens_out=self._usage.tokens_out,
            cost_usd=self._usage.cost_usd,
            model=self.model,
        )

        def _gen():
            try:
                for ch in response:
                    yield ch
            finally:
                usage_holder.append(per_call_usage)

        return _gen(), usage_holder


# ── Endpoint Security ────────────────────────────────────────────────────────

def test_metrics_endpoint_unprotected(monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    client = TestClient(app)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "chat_requests_total" in response.text


def test_metrics_endpoint_protected(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "secret-metrics-token")
    client = TestClient(app)

    # Missing token header
    response = client.get("/metrics")
    assert response.status_code == 401

    # Wrong token header
    response = client.get("/metrics", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401

    # Correct Authorization header
    response = client.get("/metrics", headers={"Authorization": "Bearer secret-metrics-token"})
    assert response.status_code == 200
    assert "chat_requests_total" in response.text

    # Correct X-Metrics-Token header
    response = client.get("/metrics", headers={"X-Metrics-Token": "secret-metrics-token"})
    assert response.status_code == 200
    assert "chat_requests_total" in response.text


# ── Middleware Timing and Counts ─────────────────────────────────────────────

def test_request_logging_middleware_metrics(monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    client = TestClient(app)

    # Gather baseline values
    base_other_success = REGISTRY.get_sample_value("chat_requests_total", {"route": "other", "status": "success"}) or 0.0
    base_other_client_error = REGISTRY.get_sample_value("chat_requests_total", {"route": "other", "status": "client_error"}) or 0.0
    base_other_server_error = REGISTRY.get_sample_value("chat_requests_total", {"route": "other", "status": "server_error"}) or 0.0
    base_other_client_error_duration = REGISTRY.get_sample_value("chat_request_duration_seconds_count", {"route": "other", "status": "client_error"}) or 0.0

    # 1. Trigger a 404 (client_error)
    client.get("/api/does-not-exist")
    
    # 2. Trigger a 500 (server_error)
    client.get("/api/test-error-500")

    # 3. Trigger a 200 (success) on a non-metrics page (e.g. check /health endpoint if open)
    client.get("/api/health")

    # 4. Trigger /metrics (should NOT self-instrument)
    client.get("/metrics")

    # Assert increments
    assert REGISTRY.get_sample_value("chat_requests_total", {"route": "other", "status": "client_error"}) == base_other_client_error + 1.0
    assert REGISTRY.get_sample_value("chat_requests_total", {"route": "other", "status": "server_error"}) == base_other_server_error + 1.0
    assert REGISTRY.get_sample_value("chat_requests_total", {"route": "other", "status": "success"}) == base_other_success + 1.0
    assert REGISTRY.get_sample_value("chat_request_duration_seconds_count", {"route": "other", "status": "client_error"}) == base_other_client_error_duration + 1.0
    
    # /metrics must not self-instrument
    assert REGISTRY.get_sample_value("chat_requests_total", {"route": "metrics", "status": "success"}) is None


# ── LLM Usage Tracking (UsageTracker) ──────────────────────────────────────────

def test_usage_tracker_publishes_tokens_and_cost():
    model_name = "gemini-3.1-flash-lite"
    
    # Gather baseline values
    base_in = REGISTRY.get_sample_value("llm_tokens_total", {"model": model_name, "surface": "react", "direction": "in"}) or 0.0
    base_cached_in = REGISTRY.get_sample_value("llm_tokens_total", {"model": model_name, "surface": "react", "direction": "cached_in"}) or 0.0
    base_out = REGISTRY.get_sample_value("llm_tokens_total", {"model": model_name, "surface": "react", "direction": "out"}) or 0.0
    base_cost = REGISTRY.get_sample_value("llm_cost_usd_total", {"model": model_name}) or 0.0

    tracker = react.UsageTracker()
    usage = TokenUsage(
        tokens_in=1500,
        cached_tokens_in=400,
        tokens_out=300,
        cost_usd=0.0125,
        model=model_name
    )
    tracker.add(usage)

    # Assert increments (fresh in = tokens_in - cached_tokens_in)
    assert REGISTRY.get_sample_value("llm_tokens_total", {"model": model_name, "surface": "react", "direction": "in"}) == base_in + 1100.0
    assert REGISTRY.get_sample_value("llm_tokens_total", {"model": model_name, "surface": "react", "direction": "cached_in"}) == base_cached_in + 400.0
    assert REGISTRY.get_sample_value("llm_tokens_total", {"model": model_name, "surface": "react", "direction": "out"}) == base_out + 300.0
    assert REGISTRY.get_sample_value("llm_cost_usd_total", {"model": model_name}) == base_cost + 0.0125


# ── ReAct Loop Iterations and Tool Dispatch Telemetry ─────────────────────────

def test_react_loop_iteration_and_tool_metrics(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "metrics-react.db"))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    db.init_db()

    user = db.create_user(username="metricuser", password_hash="hash")
    db.upsert_session("metric-session", user_id=user["id"])

    from agent_run_recorder import AgentRunRecorder
    recorder = AgentRunRecorder.start(
        session_id="metric-session", user_id=user["id"], route="react_test_route", model="gemini-3.1-flash-lite",
        system_prompt_sha="sha1", react_system_sha="sha2", tool_registry_sha="sha3"
    )

    provider = CostEmittingProvider(
        scripted_responses=[
            '{"thought":"Need to run get_pods","action":"get_pods","params":{"namespace":"default"}}',
            '{"thought":"Now I answer","action":"answer","answer":"Done!"}',
            "Done!"
        ],
        usage_per_call=TokenUsage(
            tokens_in=100, cached_tokens_in=0, tokens_out=50, cost_usd=0.001, model="gemini-3.1-flash-lite"
        )
    )

    calls = []
    def dispatch_fn(tool, params):
        calls.append((tool, params))
        return {"success": True}

    # Baselines
    base_iterations_count = REGISTRY.get_sample_value("react_iterations_count", {"route": "react_test_route"}) or 0.0
    base_tool_dispatch_count = REGISTRY.get_sample_value("tool_dispatch_total", {"tool": "get_pods", "status": "success"}) or 0.0
    base_tool_dispatch_duration_count = REGISTRY.get_sample_value("tool_dispatch_duration_seconds_count", {"tool": "get_pods", "status": "success"}) or 0.0

    result = react.react_loop(
        question="run metrics test loop",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        run_recorder=recorder,
    )

    assert result.error is None
    assert "Done!" in result.answer

    # Assert iteration count histogram observation increment
    assert REGISTRY.get_sample_value("react_iterations_count", {"route": "react_test_route"}) == base_iterations_count + 1.0

    # Assert tool dispatch metric increment
    assert REGISTRY.get_sample_value("tool_dispatch_total", {"tool": "get_pods", "status": "success"}) == base_tool_dispatch_count + 1.0
    assert REGISTRY.get_sample_value("tool_dispatch_duration_seconds_count", {"tool": "get_pods", "status": "success"}) == base_tool_dispatch_duration_count + 1.0


# ── Verification Sub-Run Telemetry ───────────────────────────────────────────

def test_verification_sub_run_metrics(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "metrics-verification.db"))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    db.init_db()

    user = db.create_user(username="metricuser2", password_hash="hash")
    db.upsert_session("metric-session2", user_id=user["id"])

    from agent_run_recorder import AgentRunRecorder
    parent_recorder = AgentRunRecorder.start(
        session_id="metric-session2", user_id=user["id"], route="react", model="gemini-3.1-flash-lite",
        system_prompt_sha="sha1", react_system_sha="sha2", tool_registry_sha="sha3"
    )

    provider = CostEmittingProvider(
        scripted_responses=[
            '{"verified": true, "rationale": "everything looks clean"}'
        ],
        usage_per_call=TokenUsage(
            tokens_in=50, cached_tokens_in=0, tokens_out=25, cost_usd=0.0001, model="gemini-3.1-flash-lite"
        )
    )

    calls = []
    def dispatch_fn(tool, params):
        calls.append((tool, params))
        return {"success": True, "pods": []}

    base_tool_dispatch_count = REGISTRY.get_sample_value("tool_dispatch_total", {"tool": "get_pods", "status": "success"}) or 0.0

    react.run_verification_sub_run(
        parent_run_id=parent_recorder.run_id,
        action="delete_pod",
        params={"namespace": "default", "pod_name": "foo"},
        dispatch_fn=dispatch_fn,
        provider=provider,
        parent_recorder=parent_recorder,
        context_mgr=None
    )

    assert len(calls) == 2
    assert calls[0][0] == "get_pods"
    assert calls[1][0] == "get_events"

    # Assert get_pods was counted
    assert REGISTRY.get_sample_value("tool_dispatch_total", {"tool": "get_pods", "status": "success"}) == base_tool_dispatch_count + 1.0


# ── Synthesis Critic Fail-Open Integration ────────────────────────────────────

def test_synthesis_critic_fallback_metrics():
    # Baselines for all four checks
    base_evidence = REGISTRY.get_sample_value("critic_fallback_total", {"check_name": "evidence_supported"}) or 0.0
    base_contradiction = REGISTRY.get_sample_value("critic_fallback_total", {"check_name": "no_contradiction"}) or 0.0
    base_recency = REGISTRY.get_sample_value("critic_fallback_total", {"check_name": "recency_correct"}) or 0.0
    base_confidence = REGISTRY.get_sample_value("critic_fallback_total", {"check_name": "confidence_honest"}) or 0.0

    # Trigger fail-open via None provider
    res_none = run_synthesis_critic(
        provider=None,
        question="test",
        envelopes=[],
        retrieval_context="test",
        answer="test"
    )
    for key in ["evidence_supported", "no_contradiction", "recency_correct", "confidence_honest"]:
        assert res_none[key]["passed"] is True

    # Assert increments
    assert REGISTRY.get_sample_value("critic_fallback_total", {"check_name": "evidence_supported"}) == base_evidence + 1.0
    assert REGISTRY.get_sample_value("critic_fallback_total", {"check_name": "no_contradiction"}) == base_contradiction + 1.0
    assert REGISTRY.get_sample_value("critic_fallback_total", {"check_name": "recency_correct"}) == base_recency + 1.0
    assert REGISTRY.get_sample_value("critic_fallback_total", {"check_name": "confidence_honest"}) == base_confidence + 1.0

    # Trigger fail-open via malformed/missing keys response from provider
    class MalformedResponseProvider:
        def generate(self, prompt, system=None, temperature=0.2, max_tokens=None):
            # Missing confidence_honest key entirely
            return '{"evidence_supported": {"passed": true, "rationale": "ok"}, "no_contradiction": {"passed": true, "rationale": "ok"}, "recency_correct": {"passed": true, "rationale": "ok"}}'

    res_malformed = run_synthesis_critic(
        provider=MalformedResponseProvider(),
        question="test",
        envelopes=[],
        retrieval_context="test",
        answer="test"
    )
    # Check that missing key fallback runs and passes by default
    assert res_malformed["confidence_honest"]["passed"] is True

    # Assert that only confidence_honest fallback metric incremented in this run
    assert REGISTRY.get_sample_value("critic_fallback_total", {"check_name": "confidence_honest"}) == base_confidence + 2.0
    assert REGISTRY.get_sample_value("critic_fallback_total", {"check_name": "evidence_supported"}) == base_evidence + 1.0
