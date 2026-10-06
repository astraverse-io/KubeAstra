"""Native tool-calling capability contract on the LLMProvider base.

Covers the opt-in surface added for Harness v2: the ToolCall/ToolTurn value
types and the default behavior a provider inherits when it does NOT implement
native tools (supports_native_tools False, generate_with_tools raises).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.llm.base import LLMProvider, ToolCall, ToolTurn  # noqa: E402
from services.llm.pricing import TokenUsage  # noqa: E402


class _BareProvider(LLMProvider):
    """Minimal provider that implements only the required text path."""

    name = "bare"
    model = "fake-model"

    @property
    def enabled(self) -> bool:
        return True

    def generate(self, prompt, system=None, temperature=0.2, max_tokens=None):
        return "ok"


def test_tool_call_holds_fields():
    tc = ToolCall(id="call_1", name="get_pods", arguments={"namespace": "default"})
    assert tc.id == "call_1"
    assert tc.name == "get_pods"
    assert tc.arguments == {"namespace": "default"}


def test_tool_turn_defaults_stop_to_end():
    turn = ToolTurn(text="hi", tool_calls=[], usage=TokenUsage.empty(model="fake-model"))
    assert turn.stop == "end"
    assert turn.tool_calls == []


def test_provider_defaults_to_no_native_tools():
    assert _BareProvider().supports_native_tools() is False


def test_generate_with_tools_not_implemented_by_default():
    p = _BareProvider()
    with pytest.raises(NotImplementedError, match="bare"):
        p.generate_with_tools(messages=[{"role": "user", "content": "hi"}], tools=[])
