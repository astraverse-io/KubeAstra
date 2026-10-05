"""Agent Harness v2 — native tool-calling loop (design §6).

Exercises the loop with a scripted fake provider and an injected tool executor
(no network, no FastAPI app). Covers the tool-call→observation→answer cycle,
direct answers, tool errors fed back as observations, the step cap + forced
conclusion, usage accumulation, hook callbacks, message threading, and the
misroute guard.
"""

from __future__ import annotations

from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import pytest  # noqa: E402

from harness.loop import (  # noqa: E402
    HarnessUnsupported,
    StepRecord,
    run_native_tool_loop,
)
from services.llm.base import ToolCall, ToolTurn  # noqa: E402
from services.llm.pricing import TokenUsage  # noqa: E402

_TOOLS = [{"name": "get_pods", "description": "List pods", "input_schema": {"type": "object"}}]


def _usage(tin=10, tout=5):
    return TokenUsage(tokens_in=tin, tokens_out=tout, model="fake")


class _ScriptedProvider:
    """Returns pre-scripted ToolTurns in order; records calls it received."""

    name = "scripted"

    def __init__(self, turns, native=True):
        self._turns = list(turns)
        self._native = native
        self.calls: list[dict] = []

    def supports_native_tools(self):
        return self._native

    def generate_with_tools(self, messages, tools, system=None, max_tokens=None):
        # Deep-ish snapshot of what the loop fed us this turn.
        self.calls.append({"messages": list(messages), "tools": tools, "system": system})
        return self._turns.pop(0)


def test_tool_call_then_answer():
    provider = _ScriptedProvider(
        [
            ToolTurn(
                text="checking",
                tool_calls=[ToolCall(id="c1", name="get_pods", arguments={"namespace": "x"})],
                usage=_usage(10, 5),
                stop="tool_calls",
            ),
            ToolTurn(text="All good.", tool_calls=[], usage=_usage(4, 2), stop="end"),
        ]
    )
    seen = []

    def execute(name, args):
        seen.append((name, args))
        return "pod a: Running"

    result = run_native_tool_loop(
        provider=provider, question="status?", tools=_TOOLS, execute_tool=execute
    )

    assert result.halted == "answered"
    assert result.answer == "All good."
    assert seen == [("get_pods", {"namespace": "x"})]
    assert len(result.steps) == 1
    assert result.steps[0].observation == "pod a: Running"
    # Usage summed across both provider calls.
    assert result.usage.tokens_in == 14 and result.usage.tokens_out == 7


def test_direct_answer_has_no_steps():
    provider = _ScriptedProvider(
        [ToolTurn(text="42", tool_calls=[], usage=_usage(3, 1), stop="end")]
    )
    result = run_native_tool_loop(
        provider=provider, question="q", tools=_TOOLS, execute_tool=lambda n, a: "x"
    )
    assert result.halted == "answered"
    assert result.answer == "42"
    assert result.steps == []


def test_empty_response_is_flagged():
    provider = _ScriptedProvider(
        [ToolTurn(text="", tool_calls=[], usage=_usage(1, 0), stop="end")]
    )
    result = run_native_tool_loop(
        provider=provider, question="q", tools=_TOOLS, execute_tool=lambda n, a: "x"
    )
    assert result.halted == "empty"
    assert result.answer == ""


def test_tool_error_is_fed_back_not_fatal():
    provider = _ScriptedProvider(
        [
            ToolTurn(
                text="try",
                tool_calls=[ToolCall(id="c1", name="boom", arguments={})],
                usage=_usage(),
                stop="tool_calls",
            ),
            ToolTurn(text="recovered", tool_calls=[], usage=_usage(), stop="end"),
        ]
    )

    def execute(name, args):
        raise RuntimeError("kaboom")

    result = run_native_tool_loop(
        provider=provider, question="q", tools=_TOOLS, execute_tool=execute
    )
    assert result.answer == "recovered"
    assert result.steps[0].error is True
    assert "kaboom" in result.steps[0].observation
    # The error observation was threaded back as a tool message.
    tool_msgs = [m for m in provider.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "kaboom" in tool_msgs[0]["content"]


def test_step_cap_forces_tool_free_conclusion():
    # Always ask for a tool; the loop must stop at max_steps and force an answer.
    # Supply exactly max_steps tool-turns, then the forced tool-free conclusion.
    looping = [
        ToolTurn(
            text="again",
            tool_calls=[ToolCall(id=f"c{i}", name="get_pods", arguments={})],
            usage=_usage(1, 1),
            stop="tool_calls",
        )
        for i in range(3)
    ]
    forced = ToolTurn(text="final best effort", tool_calls=[], usage=_usage(2, 2), stop="end")
    provider = _ScriptedProvider(looping + [forced])

    result = run_native_tool_loop(
        provider=provider,
        question="q",
        tools=_TOOLS,
        execute_tool=lambda n, a: "obs",
        max_steps=3,
    )
    assert result.halted == "max_steps"
    assert result.answer == "final best effort"
    assert len(result.steps) == 3  # exactly the cap
    # The forced final call was made with NO tools.
    assert provider.calls[-1]["tools"] == []


def test_hooks_fire():
    provider = _ScriptedProvider(
        [
            ToolTurn(
                text="t",
                tool_calls=[ToolCall(id="c1", name="get_pods", arguments={})],
                usage=_usage(),
                stop="tool_calls",
            ),
            ToolTurn(text="done", tool_calls=[], usage=_usage(), stop="end"),
        ]
    )
    steps_seen: list[StepRecord] = []
    answers: list[str] = []
    run_native_tool_loop(
        provider=provider,
        question="q",
        tools=_TOOLS,
        execute_tool=lambda n, a: "obs",
        on_step=steps_seen.append,
        on_answer=answers.append,
    )
    assert len(steps_seen) == 1 and steps_seen[0].tool_name == "get_pods"
    assert answers == ["done"]


def test_assistant_turn_threaded_with_tool_calls():
    tc = ToolCall(id="c1", name="get_pods", arguments={"namespace": "x"})
    provider = _ScriptedProvider(
        [
            ToolTurn(text="checking", tool_calls=[tc], usage=_usage(), stop="tool_calls"),
            ToolTurn(text="done", tool_calls=[], usage=_usage(), stop="end"),
        ]
    )
    run_native_tool_loop(
        provider=provider, question="q", tools=_TOOLS, execute_tool=lambda n, a: "obs"
    )
    # Second provider call should have seen: user, assistant(+tool_calls), tool.
    second = provider.calls[1]["messages"]
    assert second[0]["role"] == "user"
    assert second[1]["role"] == "assistant" and second[1]["tool_calls"] == [tc]
    assert second[2] == {"role": "tool", "tool_call_id": "c1", "name": "get_pods", "content": "obs"}


def test_unsupported_provider_raises():
    provider = _ScriptedProvider([], native=False)
    with pytest.raises(HarnessUnsupported, match="text-ReAct"):
        run_native_tool_loop(
            provider=provider, question="q", tools=_TOOLS, execute_tool=lambda n, a: "x"
        )


def test_nonpositive_max_steps_rejected():
    provider = _ScriptedProvider([])
    with pytest.raises(ValueError, match="max_steps"):
        run_native_tool_loop(
            provider=provider, question="q", tools=_TOOLS, execute_tool=lambda n, a: "x", max_steps=0
        )
