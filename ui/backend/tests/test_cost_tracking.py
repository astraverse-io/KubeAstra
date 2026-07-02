"""Phase 1A — cost tracking end-to-end.

Verifies:
- ``TokenUsage`` accumulates correctly across calls.
- ``compute_cost`` applies the cached-token discount (``CACHE_RATE``).
- Unknown models log a warning and return 0 cost.
- A ReAct run with a usage-emitting fake provider lands tokens + cost in the
  per-step and per-run DB rows.
- ``REACT_SYSTEM_SHA`` / ``TOOL_REGISTRY_SHA`` are non-empty strings and stable
  across calls.
"""
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import logging

import db
import react
from services.llm.pricing import (
    CACHE_RATE,
    PRICE_PER_1K_TOKENS,
    TokenUsage,
    compute_cost,
)


def _init_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "cost-tracking.db"))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    db.init_db()


def _seed_user_and_session() -> tuple[str, str]:
    user = db.create_user(username="costuser", password_hash="hash")
    db.upsert_session("cost-session", user_id=user["id"])
    return user["id"], "cost-session"


# ── TokenUsage + compute_cost ─────────────────────────────────────────────────

def test_token_usage_addition_accumulates_fields():
    a = TokenUsage(tokens_in=100, cached_tokens_in=20, tokens_out=50, cost_usd=0.001, model="gemini-3.1-flash-lite")
    b = TokenUsage(tokens_in=200, cached_tokens_in=10, tokens_out=80, cost_usd=0.002, model="gemini-3.1-flash-lite")
    c = a + b
    assert c.tokens_in == 300
    assert c.cached_tokens_in == 30
    assert c.tokens_out == 130
    assert c.cost_usd == 0.003
    assert c.model == "gemini-3.1-flash-lite"


def test_compute_cost_applies_cache_rate_discount():
    """Cached input tokens are billed at CACHE_RATE of the fresh-input rate."""
    model = "gemini-3.1-flash-lite"
    rates = PRICE_PER_1K_TOKENS[model]
    usage = TokenUsage(tokens_in=10_000, cached_tokens_in=4_000, tokens_out=500, model=model)
    expected = (
        (10_000 - 4_000) * rates["in"] / 1000          # fresh
        + 4_000 * rates["in"] * CACHE_RATE / 1000       # cached at 25%
        + 500 * rates["out"] / 1000                      # output
    )
    assert abs(compute_cost(usage) - expected) < 1e-9


def test_compute_cost_unknown_model_logs_warning_and_returns_zero(caplog):
    caplog.set_level(logging.WARNING, logger="services.llm.pricing")
    usage = TokenUsage(tokens_in=1000, tokens_out=500, model="not-a-real-model")
    cost = compute_cost(usage)
    assert cost == 0.0
    assert any("unpriced model" in rec.message for rec in caplog.records)


def test_compute_cost_local_model_is_zero():
    """Local Ollama models are priced at zero in PRICE_PER_1K_TOKENS."""
    usage = TokenUsage(tokens_in=100_000, tokens_out=20_000, model="llama3.1")
    assert compute_cost(usage) == 0.0


# ── UsageTracker step / total separation ──────────────────────────────────────

def test_usage_tracker_take_step_resets_per_step_counter():
    tracker = react.UsageTracker()
    tracker.add(TokenUsage(tokens_in=100, tokens_out=50, cost_usd=0.01, model="gemini-3.1-flash-lite"))
    tracker.add(TokenUsage(tokens_in=200, tokens_out=80, cost_usd=0.02, model="gemini-3.1-flash-lite"))
    step1 = tracker.take_step()
    assert step1.tokens_in == 300
    assert step1.tokens_out == 130
    assert abs(step1.cost_usd - 0.03) < 1e-9

    tracker.add(TokenUsage(tokens_in=50, tokens_out=25, cost_usd=0.005, model="gemini-3.1-flash-lite"))
    step2 = tracker.take_step()
    assert step2.tokens_in == 50
    assert step2.tokens_out == 25
    # Total is the cumulative sum across all calls — independent of take_step resets.
    assert tracker.total.tokens_in == 350
    assert tracker.total.tokens_out == 155
    assert abs(tracker.total.cost_usd - 0.035) < 1e-9


# ── Prompt SHA bundling ───────────────────────────────────────────────────────

def test_prompt_shas_are_populated_and_stable():
    assert react.REACT_SYSTEM_SHA
    assert react.TOOL_REGISTRY_SHA
    assert react.SYSTEM_PROMPT_SHA
    # 16 hex chars from sha256[:16]
    assert len(react.REACT_SYSTEM_SHA) == 16
    assert len(react.TOOL_REGISTRY_SHA) == 16
    # Stability across re-imports — same string, same SHA.
    import hashlib
    expected_react = hashlib.sha256(react.REACT_SYSTEM.encode("utf-8")).hexdigest()[:16]
    assert react.REACT_SYSTEM_SHA == expected_react


# ── End-to-end: usage-emitting provider lands in DB ───────────────────────────

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


def test_react_run_records_per_step_and_total_cost(monkeypatch, tmp_path):
    """Run react_loop with the cost-emitting provider; assert agent_runs row
    has total_cost_usd > 0 and agent_steps rows carry per-step tokens."""
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    from agent_run_recorder import AgentRunRecorder

    provider = CostEmittingProvider(
        scripted_responses=[
            '{"thought":"Check the pods","action":"get_pods","params":{"namespace":"default"}}',
            '{"thought":"All good","action":"answer","answer":"Done."}',
            "Done.",
        ],
        usage_per_call=TokenUsage(
            tokens_in=1000, cached_tokens_in=200, tokens_out=50,
            cost_usd=compute_cost(TokenUsage(
                tokens_in=1000, cached_tokens_in=200, tokens_out=50, model="gemini-3.1-flash-lite"
            )),
            model="gemini-3.1-flash-lite",
        ),
    )

    def fake_dispatch(tool, params):
        return {"success": True, "pods": []}

    recorder = AgentRunRecorder.start(
        session_id=sid, user_id=uid, route="react", model=provider.model,
        system_prompt_sha=react.SYSTEM_PROMPT_SHA,
        react_system_sha=react.REACT_SYSTEM_SHA,
        tool_registry_sha=react.TOOL_REGISTRY_SHA,
    )
    run_id = recorder.run_id

    result = react.react_loop(
        question="check my pods",
        history=[],
        provider=provider,
        dispatch_fn=fake_dispatch,
        run_recorder=recorder,
    )

    assert result.error is None
    assert "Done." in result.answer

    run = db.get_agent_run(run_id)
    assert run is not None

    # Run-level rollup: at least the two LLM calls (think + answer) each emit
    # the scripted usage, so total_tokens_in must be >= 2 * 1000.
    assert run["total_tokens_in"] >= 2000, f"expected >=2000 tokens_in, got {run['total_tokens_in']}"
    assert run["total_tokens_out"] >= 100, f"expected >=100 tokens_out, got {run['total_tokens_out']}"
    assert run["total_cost_usd"] > 0, f"expected nonzero total_cost_usd, got {run['total_cost_usd']}"
    assert run["total_cached_tokens_in"] >= 400

    # Prompt SHAs landed.
    assert run["system_prompt_sha"] == react.SYSTEM_PROMPT_SHA
    assert run["react_system_sha"] == react.REACT_SYSTEM_SHA
    assert run["tool_registry_sha"] == react.TOOL_REGISTRY_SHA

    # Per-step: at least one step has non-zero tokens_in.
    steps = db.get_agent_steps(run_id)
    assert any(s["tokens_in"] > 0 for s in steps), \
        f"no per-step tokens_in recorded; steps={[(s['action'], s['tokens_in']) for s in steps]}"


def test_safety_review_and_critic_accumulates_usage(monkeypatch, tmp_path):
    """Verify that safety reviews and critic checks contribute to the UsageTracker and final run cost."""
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    from agent_run_recorder import AgentRunRecorder
    from services.tool_envelope import ToolEnvelope

    # Setup a mock provider that returns structured JSON for the ReAct loop, 
    # then critic checks, then safety review.
    # We will verify that usage is recorded at both run and step levels.
    provider = CostEmittingProvider(
        scripted_responses=[
            # 1. ReAct iteration 1: thought + action to delete pod
            '{"thought":"Need to delete pod","action":"delete_pod","params":{"namespace":"default","pod_name":"nginx"}}',
            # 2. ReAct iteration 2: thought + action to answer
            '{"thought":"All good","action":"answer","answer":"Done."}',
            # 3. Synchronous finalize answer in pre-gated/critic flow (triggered by low confidence)
            '# Diagnosis\nDelete nginx pod\n# Evidence\n* envelope[0] - ok\n# Recommended Actions\n`kubectl delete pod nginx -n default` (requires approval)',
            # 4. Critic check response (must be valid JSON for run_synthesis_critic)
            '{"evidence_supported":{"passed":true,"rationale":"ok"},"no_contradiction":{"passed":true,"rationale":"ok"},"recency_correct":{"passed":true,"rationale":"ok"},"confidence_honest":{"passed":true,"rationale":"ok"}}',
            # 5. LLM safety review response (must be valid JSON for _llm_review_recovery_action)
            '{"approved":true,"reason":"safe to delete"}',
        ],
        usage_per_call=TokenUsage(
            tokens_in=500, cached_tokens_in=100, tokens_out=50,
            cost_usd=0.001,
            model="gemini-3.1-flash-lite",
        ),
    )

    def fake_dispatch(tool, params):
        return {"success": True, "message": "deleted"}

    # Mock confidence report to band='low' and envelopes non-empty to trigger pre-gating flow
    monkeypatch.setattr(react, "compute_confidence_report", lambda envelopes, budget_exhausted: {
        "band": "low", "reasons": ["stale"]
    })

    # Mock deterministic action review to approve the command so LLM safety review is called
    monkeypatch.setattr(react, "_deterministic_review_recovery_action", lambda action, evidence_priority: {
        "approved": True,
        "action_kind": "write_command",
        "risk": "low",
        "evidence_reference": {},
    })

    recorder = AgentRunRecorder.start(
        session_id=sid, user_id=uid, route="react", model=provider.model,
        system_prompt_sha=react.SYSTEM_PROMPT_SHA,
        react_system_sha=react.REACT_SYSTEM_SHA,
        tool_registry_sha=react.TOOL_REGISTRY_SHA,
    )
    run_id = recorder.run_id

    result = react.react_loop(
        question="delete pod nginx",
        history=[],
        provider=provider,
        dispatch_fn=fake_dispatch,
        run_recorder=recorder,
    )

    assert result.error is None

    run = db.get_agent_run(run_id)
    assert run is not None
    # We should have at least:
    # - 1 ReAct iteration 1 LLM call (500)
    # - 1 ReAct iteration 2 LLM call (500)
    # - 1 finalize LLM call (500)
    # - 1 critic LLM call (500)
    # - 1 safety review LLM call (500)
    # Total calls: >= 5. Each emits 500 tokens_in, so total >= 2500.
    assert run["total_tokens_in"] >= 2500, f"Expected total_tokens_in >= 2500, got {run['total_tokens_in']}"
    assert run["total_cost_usd"] >= 0.005, f"Expected total_cost_usd >= 0.005, got {run['total_cost_usd']}"


def test_chat_endpoint_cost_summary(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    from fastapi.testclient import TestClient
    from main import app
    from routers import chat as chat_router
    client = TestClient(app)

    # Mock the LLM provider in chat router
    provider = CostEmittingProvider(
        scripted_responses=[
            '{"thought":"All good","action":"answer","answer":"Done."}',
            "Done.",
        ],
        usage_per_call=TokenUsage(
            tokens_in=100, cached_tokens_in=20, tokens_out=50, cost_usd=0.0001,
            model="gemini-3.1-flash-lite"
        )
    )
    monkeypatch.setattr(chat_router, "_llm_provider", lambda model=None: provider)

    # 1. Test with SHOW_COST_TO_USERS=true
    monkeypatch.setenv("SHOW_COST_TO_USERS", "true")
    response = client.post("/api/chat", json={
        "message": "check pods",
        "session_id": sid,
    })
    assert response.status_code == 200
    data = response.json()
    assert data["cost_summary"] is not None
    assert data["cost_summary"]["total_tokens_in"] >= 100
    assert data["cost_summary"]["total_cost_usd"] > 0
    assert data["cost_summary"]["model"] == "gemini-3.1-flash-lite"

    # 2. Test with SHOW_COST_TO_USERS=false
    monkeypatch.setenv("SHOW_COST_TO_USERS", "false")
    response2 = client.post("/api/chat", json={
        "message": "check pods again",
        "session_id": sid,
    })
    assert response2.status_code == 200
    data2 = response2.json()
    assert data2["cost_summary"] is None


def test_chat_endpoint_cost_summary_unset_default_true(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    from fastapi.testclient import TestClient
    from main import app
    from routers import chat as chat_router
    client = TestClient(app)

    provider = CostEmittingProvider(
        scripted_responses=[
            '{"thought":"All good","action":"answer","answer":"Done."}',
            "Done.",
        ],
        usage_per_call=TokenUsage(
            tokens_in=100, cached_tokens_in=20, tokens_out=50, cost_usd=0.0001,
            model="gemini-3.1-flash-lite"
        )
    )
    monkeypatch.setattr(chat_router, "_llm_provider", lambda model=None: provider)

    monkeypatch.delenv("SHOW_COST_TO_USERS", raising=False)
    response = client.post("/api/chat", json={
        "message": "check pods default",
        "session_id": sid,
    })
    assert response.status_code == 200
    data = response.json()
    assert data["cost_summary"] is not None
    assert data["cost_summary"]["total_tokens_in"] >= 100


def test_chat_stream_endpoint_cost_summary(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    uid, sid = _seed_user_and_session()

    from fastapi.testclient import TestClient
    from main import app
    from routers import chat as chat_router
    import json
    client = TestClient(app)

    provider = CostEmittingProvider(
        scripted_responses=[
            '{"thought":"All good","action":"answer","answer":"Done."}',
            "Done.",
        ],
        usage_per_call=TokenUsage(
            tokens_in=100, cached_tokens_in=20, tokens_out=50, cost_usd=0.0001,
            model="gemini-3.1-flash-lite"
        )
    )
    monkeypatch.setattr(chat_router, "_llm_provider", lambda model=None: provider)
    monkeypatch.setenv("SHOW_COST_TO_USERS", "true")

    response = client.post("/api/chat/stream", json={
        "message": "check pods stream",
        "session_id": sid,
    })
    assert response.status_code == 200
    
    done_event = None
    for line in response.iter_lines():
        decoded = line.decode("utf-8") if isinstance(line, bytes) else line
        if decoded.startswith("data:"):
            payload = json.loads(decoded[5:].strip())
            if payload.get("type") == "done":
                done_event = payload
                break

    assert done_event is not None
    result = done_event.get("result")
    assert result is not None
    assert result.get("cost_summary") is not None
    assert result["cost_summary"]["total_tokens_in"] >= 100
    assert result["cost_summary"]["total_cost_usd"] > 0



