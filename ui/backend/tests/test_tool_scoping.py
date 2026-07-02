"""Tool scoper + react_loop scope integration tests (harness Phase 7)."""

from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import db  # noqa: E402
from tool_scoper import (  # noqa: E402
    ESCAPE_HATCHES,
    SCOPE_TOOLS,
    ScopeDecision,
    scope_for_prompt,
)


# ── Classifier unit tests ────────────────────────────────────────────────────

def test_scope_pod_debug_keywords():
    """Pod-shaped failure prompts classify as pod_debug."""
    for prompt in [
        "I have a pod in CrashLoopBackOff in the default namespace",
        "ImagePullBackOff on the api-server pod",
        "Why is the backend-app pod crashing in prod-blue?",
        "Investigate api-pod in default. It was reported as failing.",
        "Analyze the logs of the auth-server pod in prod-blue",
        "OOM kill on the worker pod",
        "Liveness probe is failing repeatedly",
    ]:
        d = scope_for_prompt(prompt)
        assert d.scope_name == "pod_debug", f"expected pod_debug for: {prompt!r}, got {d.scope_name}"
        # Escape hatches always included.
        assert ESCAPE_HATCHES.issubset(d.allowed_tools)


def test_scope_rollout_keywords():
    for prompt in [
        "Check the rollout status of the frontend deployment in prod-blue",
        "Deployment is stuck rolling out",
        "Are all replicas ready for the frontend deployment?",
        "Show me the replicas for the cart service",
    ]:
        d = scope_for_prompt(prompt)
        assert d.scope_name == "rollout", f"expected rollout for: {prompt!r}, got {d.scope_name}"


def test_scope_service_keywords():
    for prompt in [
        "The api service has no endpoints — what's going on?",
        "service frontend-service is returning 503 errors. Check its endpoints.",
        "Ingress not routing traffic to the new pods",
        "Endpoints missing for the cart service",
    ]:
        d = scope_for_prompt(prompt)
        assert d.scope_name == "service", f"expected service for: {prompt!r}, got {d.scope_name}"


def test_scope_inventory_keywords():
    for prompt in [
        "List all namespaces in the cluster",
        "Show me the pods in default",
        "What namespaces exist?",
        "How many nodes do we have?",
    ]:
        d = scope_for_prompt(prompt)
        assert d.scope_name == "inventory", f"expected inventory for: {prompt!r}, got {d.scope_name}"


def test_scope_knowledge_keywords():
    for prompt in [
        "How do we recover from a Redis failover according to our runbooks?",
        "What is our standard procedure for an OOM incident?",
        "According to our team docs, how do we handle this?",
        "Ansible playbook failure on the prod-blue host",
    ]:
        d = scope_for_prompt(prompt)
        assert d.scope_name == "knowledge", f"expected knowledge for: {prompt!r}, got {d.scope_name}"


def test_scope_broad_fallback():
    """Unmatched prompts fall to broad — no restriction applied."""
    for prompt in [
        "What is in our cluster?",
        "Random non-k8s prompt about pizza recipes",
        "",  # empty
        "tell me a joke",
    ]:
        d = scope_for_prompt(prompt)
        assert d.scope_name == "broad", f"expected broad for: {prompt!r}, got {d.scope_name}"
        assert d.matched_pattern is None


def test_scope_intersects_with_available_tools():
    """When available_tools is provided, the result is constrained to it."""
    only_a_few = frozenset({"investigate_pod", "find_workload", "kb_search"})
    d = scope_for_prompt("Why is the pod crashing?", available_tools=only_a_few)
    assert d.scope_name == "pod_debug"
    assert d.allowed_tools == only_a_few  # SCOPE_TOOLS["pod_debug"] ∩ only_a_few


def test_scope_decision_serializes():
    d = scope_for_prompt("Why is the pod crashing?")
    payload = d.to_dict()
    assert payload["scope"] == "pod_debug"
    assert isinstance(payload["allowed_tools"], list)
    assert payload["allowed_tools"] == sorted(payload["allowed_tools"])  # sorted for stable JSON
    assert payload["matched_pattern"]


def test_escape_hatches_always_present_in_every_scope():
    for name in SCOPE_TOOLS:
        d = scope_for_prompt(_prompt_for_scope(name))
        if d.scope_name == name:
            assert ESCAPE_HATCHES.issubset(d.allowed_tools), \
                f"escape hatches missing from {name}: {ESCAPE_HATCHES - d.allowed_tools}"


def _prompt_for_scope(scope_name: str) -> str:
    return {
        "pod_debug": "Why is the api pod crashing?",
        "rollout": "Rollout status of the frontend deployment",
        "service": "The frontend service has no endpoints",
        "inventory": "List all namespaces",
        "knowledge": "What is our standard procedure for an OOM incident?",
    }[scope_name]


# ── react_loop integration: out-of-scope tool triggers recovery ──────────────

def _init_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "scope-test.db"))
    db.init_db()


class _FakeProvider:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def generate_stream(self, prompt, system=None, temperature=0.1, max_tokens=8000):
        self.calls += 1
        idx = min(self.calls - 1, len(self._responses) - 1)
        yield self._responses[idx]

    def generate(self, prompt, system=None, temperature=0.2, max_tokens=8000):
        return ""


def test_react_loop_blocks_out_of_scope_tool(monkeypatch, tmp_path):
    """When tool_scope is set and the model picks an out-of-scope tool, the dispatch
    is short-circuited with a tool_out_of_scope error that the agent can recover from."""
    _init_temp_db(monkeypatch, tmp_path)

    from react import react_loop

    # Iter 1: model picks 'get_resource_graph' (not in pod_debug scope)
    # Iter 2: model picks 'investigate_pod' (in scope) — dispatch succeeds
    # Iter 3: answer
    provider = _FakeProvider([
        '{"thought": "let me look at the graph", "action": "get_resource_graph", "params": {}}',
        '{"thought": "ok, investigate the pod", "action": "investigate_pod", "params": {"pod_name": "x"}}',
        '{"thought": "done", "action": "answer", "answer": "Found it."}',
    ])

    dispatched: list[str] = []
    def dispatch(action, params):
        dispatched.append(action)
        if action == "investigate_pod":
            return {"pod_name": "x", "status": "CrashLoopBackOff"}
        return {"error": "unknown_tool"}

    pod_debug_scope = {
        "investigate_pod", "get_pod_logs", "get_events",
        "find_workload", "get_namespaces", "kb_search",
    }

    result = react_loop(
        question="why is pod x crashing?",
        history=[],
        provider=provider,
        dispatch_fn=dispatch,
        max_iterations=5,
        tool_scope=pod_debug_scope,
    )

    # The out-of-scope tool was NEVER dispatched.
    assert "get_resource_graph" not in dispatched
    # The in-scope tool WAS dispatched.
    assert "investigate_pod" in dispatched
    # The agent eventually answered.
    assert "Found it" in result.answer


def test_react_loop_allows_all_tools_when_scope_is_none(monkeypatch, tmp_path):
    """Legacy callers (scope=None) get the full toolbox — no behavior change."""
    _init_temp_db(monkeypatch, tmp_path)
    from react import react_loop

    provider = _FakeProvider([
        '{"thought": "graph it", "action": "get_resource_graph", "params": {}}',
        '{"thought": "done", "action": "answer", "answer": "ok"}',
    ])
    dispatched: list[str] = []
    def dispatch(action, params):
        dispatched.append(action)
        if action == "get_resource_graph":
            return {"nodes": []}
        return {"error": "unknown_tool"}

    react_loop(
        question="show me the graph",
        history=[],
        provider=provider,
        dispatch_fn=dispatch,
        max_iterations=3,
        tool_scope=None,  # default — no scoping
    )

    assert "get_resource_graph" in dispatched


def test_out_of_scope_retry_keeps_emitting_scope_error_not_duplicate(monkeypatch, tmp_path):
    """Regression: a blocked out-of-scope tool must NOT poison executed_calls.

    If it did, a retry of the same out-of-scope tool would hit
    'duplicate_tool_call' instead of 'tool_out_of_scope', misleading the agent
    into thinking it had already gotten a real result.

    We use a service-shaped prompt so the pod-coercion layer in react.py
    (``_coerce_action_for_question``) doesn't rewrite the model's first action.
    """
    _init_temp_db(monkeypatch, tmp_path)
    from react import react_loop

    # Scope: service-only. The model stubbornly tries investigate_pod twice
    # (out-of-scope), then finally picks get_endpoints (in-scope), then answers.
    provider = _FakeProvider([
        '{"thought": "look at the pod", "action": "investigate_pod", "params": {"pod_name": "x"}}',
        '{"thought": "again, pod", "action": "investigate_pod", "params": {"pod_name": "x"}}',
        '{"thought": "ok, endpoints", "action": "get_endpoints", "params": {"name": "api"}}',
        '{"thought": "done", "action": "answer", "answer": "no endpoints found"}',
    ])
    dispatched: list[str] = []

    def dispatch(action, params):
        dispatched.append(action)
        if action == "get_endpoints":
            return {"endpoints": []}
        return {"error": "unknown_tool"}

    scope = {"get_endpoints", "get_service", "list_services",
             "find_workload", "get_namespaces", "kb_search"}

    result = react_loop(
        question="Why doesn't the api service have endpoints in prod-blue?",
        history=[],
        provider=provider,
        dispatch_fn=dispatch,
        max_iterations=6,
        tool_scope=scope,
    )

    # The out-of-scope tool was NEVER dispatched (both attempts blocked).
    assert "investigate_pod" not in dispatched
    # The in-scope tool ran exactly once.
    assert dispatched.count("get_endpoints") == 1
    # The recorded observation on both blocked iterations must show
    # 'tool_out_of_scope', never 'duplicate_tool_call'.
    by_iter = {s.iteration: s for s in result.steps}
    obs_first = (by_iter[1].observation or "")
    obs_second = (by_iter[2].observation or "")
    assert "tool_out_of_scope" in obs_first
    assert "tool_out_of_scope" in obs_second
    assert "duplicate_tool_call" not in obs_first
    assert "duplicate_tool_call" not in obs_second
