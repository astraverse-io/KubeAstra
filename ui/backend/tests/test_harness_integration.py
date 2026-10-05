"""Agent Harness v2 — integration into react_loop (PR 5).

Two layers:
1. run_native_react directly: ReActResult shape, on_event SSE events, recorder
   calls, tool_scope passthrough, observations via _truncate_observation.
2. react_loop routing: with AGENT_HARNESS_V2 on and a native provider, react_loop
   delegates to the native path; with it off (or a non-native provider) it does
   not. No network — a scripted fake provider stands in for the LLM.
"""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import pytest  # noqa: E402

from harness.integration import run_native_react  # noqa: E402
from services.llm.base import ToolCall, ToolTurn  # noqa: E402
from services.llm.pricing import TokenUsage  # noqa: E402


def _usage(tin=10, tout=5):
    return TokenUsage(tokens_in=tin, tokens_out=tout, model="fake")


class _ScriptedProvider:
    name = "scripted"
    model = "fake"

    def __init__(self, turns, native=True):
        self._turns = list(turns)
        self._native = native
        self.last_tools = None

    def supports_native_tools(self):
        return self._native

    def generate_with_tools(self, messages, tools, system=None, max_tokens=None):
        self.last_tools = tools
        return self._turns.pop(0)


def _one_call_then_answer():
    return _ScriptedProvider(
        [
            ToolTurn(
                text="checking",
                tool_calls=[ToolCall(id="c1", name="get_pods", arguments={"namespace": "x"})],
                usage=_usage(10, 5),
                stop="tool_calls",
            ),
            ToolTurn(text="All healthy.", tool_calls=[], usage=_usage(4, 2), stop="end"),
        ]
    )


def _dispatch_ok(name, params):
    return {"status": "ok", "summary": f"{name} ran", "params": params}


def test_returns_reactresult_with_steps_and_answer():
    provider = _one_call_then_answer()
    result = run_native_react(
        question="what's failing?", provider=provider, dispatch_fn=_dispatch_ok
    )
    # ReActResult contract.
    assert result.answer == "All healthy."
    assert result.tool_used == "get_pods"
    assert result.total_iterations == 1
    assert len(result.steps) == 1
    step = result.steps[0]
    assert step.action == "get_pods"
    assert step.action_params == {"namespace": "x"}
    assert step.observation  # went through _truncate_observation, non-empty
    assert result.total_duration_ms >= 0


def test_emits_step_and_answer_events():
    provider = _one_call_then_answer()
    events = []
    run_native_react(
        question="q", provider=provider, dispatch_fn=_dispatch_ok, on_event=events.append
    )
    types = [e["type"] for e in events]
    assert "step_complete" in types
    assert types.count("answer_start") == 1
    assert types[-1] == "answer_end"
    step_evt = next(e for e in events if e["type"] == "step_complete")
    assert step_evt["action"] == "get_pods"
    assert step_evt["iteration"] == 1
    assert step_evt["error"] is False
    answer_evt = next(e for e in events if e["type"] == "answer_end")
    assert answer_evt["answer"] == "All healthy."


def test_records_steps_and_finish_on_recorder():
    provider = _one_call_then_answer()
    recorder = MagicMock()
    run_native_react(
        question="q", provider=provider, dispatch_fn=_dispatch_ok, run_recorder=recorder
    )
    assert recorder.record_step.call_count == 1
    step_kwargs = recorder.record_step.call_args.kwargs
    assert step_kwargs["action"] == "get_pods"
    assert step_kwargs["status"] == "ok"
    recorder.finish.assert_called_once()
    finish_kwargs = recorder.finish.call_args.kwargs
    assert finish_kwargs["final_answer"] == "All healthy."
    assert finish_kwargs["final_tool"] == "get_pods"
    # Usage summed across both provider turns (10+4 in, 5+2 out).
    assert finish_kwargs["total_tokens_in"] == 14
    assert finish_kwargs["total_tokens_out"] == 7


def test_tool_scope_is_passed_to_spec_builder():
    provider = _ScriptedProvider(
        [ToolTurn(text="hi", tool_calls=[], usage=_usage(), stop="end")]
    )
    run_native_react(
        question="q", provider=provider, dispatch_fn=_dispatch_ok, tool_scope={"get_pods"}
    )
    # Only the scoped tool's spec reached the provider.
    assert [t["name"] for t in provider.last_tools] == ["get_pods"]


def test_tool_error_recorded_as_error_step():
    provider = _ScriptedProvider(
        [
            ToolTurn(
                text="try",
                tool_calls=[ToolCall(id="c1", name="get_pods", arguments={})],
                usage=_usage(),
                stop="tool_calls",
            ),
            ToolTurn(text="done", tool_calls=[], usage=_usage(), stop="end"),
        ]
    )

    def boom(name, params):
        raise RuntimeError("dispatch exploded")

    recorder = MagicMock()
    result = run_native_react(
        question="q", provider=provider, dispatch_fn=boom, run_recorder=recorder
    )
    assert result.steps[0].observation.startswith("ERROR:")
    assert recorder.record_step.call_args.kwargs["status"] == "error"


# ── react_loop routing ────────────────────────────────────────────────────────


def _patch_flag(monkeypatch, enabled: bool):
    fake_settings = SimpleNamespace(agent_harness_v2=enabled, agent_harness_v2_max_steps=12)
    monkeypatch.setattr("config.settings.get_settings", lambda: fake_settings)


def test_react_loop_routes_to_native_when_enabled(monkeypatch):
    import react

    _patch_flag(monkeypatch, True)
    provider = _one_call_then_answer()
    result = react.react_loop(
        question="q", history=[], provider=provider, dispatch_fn=_dispatch_ok
    )
    # The native path's answer + single tool step prove it routed here, not to
    # the text-ReAct loop (which would have parsed action JSON from our fake).
    assert result.answer == "All healthy."
    assert result.total_iterations == 1
    assert result.steps[0].action == "get_pods"


def test_react_loop_skips_native_for_nonnative_provider(monkeypatch):
    import react

    _patch_flag(monkeypatch, True)
    provider = _ScriptedProvider([], native=False)
    # Non-native provider must NOT hit the native path; it falls through to the
    # text loop, which won't call generate_with_tools (our script is empty, so a
    # native misroute would IndexError).
    try:
        react.react_loop(question="q", history=[], provider=provider, dispatch_fn=_dispatch_ok)
    except IndexError:  # pragma: no cover
        pytest.fail("react_loop wrongly routed a non-native provider to the native path")
    except Exception:
        pass  # text-ReAct path may fail for other reasons with a bare fake; that's fine
