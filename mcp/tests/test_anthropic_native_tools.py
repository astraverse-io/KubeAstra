"""Anthropic native tool-calling (generate_with_tools) — mocked SDK.

Mirrors test_llm_providers.py's convention: patch _get_client to a MagicMock
whose messages.create returns a SimpleNamespace response. Covers tool_use
parsing, direct-answer (no tool_calls), stop-reason normalization, usage
extraction, refusal, the tools kwarg mapping, and the provider-neutral →
Anthropic message mapping (incl. assistant tool_calls and tool results).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.llm.anthropic_provider import AnthropicProvider  # noqa: E402
from services.llm.base import LLMProviderError, ToolCall  # noqa: E402

_TOOLS = [
    {
        "name": "get_pods",
        "description": "List pods",
        "input_schema": {"type": "object", "properties": {"namespace": {"type": "string"}}},
    }
]


def _provider() -> AnthropicProvider:
    return AnthropicProvider(api_key="sk-ant-x", model="claude-opus-4-8")


def test_supports_native_tools_true():
    assert _provider().supports_native_tools() is True


def test_parses_tool_use_block():
    p = _provider()
    fake_response = SimpleNamespace(
        stop_reason="tool_use",
        content=[
            SimpleNamespace(type="text", text="Let me check the pods."),
            SimpleNamespace(
                type="tool_use",
                id="toolu_123",
                name="get_pods",
                input={"namespace": "kube-system"},
            ),
        ],
        usage=SimpleNamespace(input_tokens=50, output_tokens=12, cache_read_input_tokens=0),
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response
    with patch.object(p, "_get_client", return_value=fake_client):
        turn = p.generate_with_tools(
            messages=[{"role": "user", "content": "what pods are failing?"}],
            tools=_TOOLS,
            system="be terse",
        )

    assert turn.text == "Let me check the pods."
    assert turn.stop == "tool_calls"
    assert len(turn.tool_calls) == 1
    call = turn.tool_calls[0]
    assert (call.id, call.name, call.arguments) == (
        "toolu_123",
        "get_pods",
        {"namespace": "kube-system"},
    )
    assert turn.usage.tokens_in == 50 and turn.usage.tokens_out == 12

    kwargs = fake_client.messages.create.call_args.kwargs
    assert kwargs["system"] == "be terse"
    assert kwargs["tools"] == [
        {
            "name": "get_pods",
            "description": "List pods",
            "input_schema": _TOOLS[0]["input_schema"],
        }
    ]
    assert kwargs["messages"] == [{"role": "user", "content": "what pods are failing?"}]


def test_direct_answer_has_no_tool_calls():
    p = _provider()
    fake_response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="All pods are healthy.")],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0),
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response
    with patch.object(p, "_get_client", return_value=fake_client):
        turn = p.generate_with_tools(messages=[{"role": "user", "content": "status?"}], tools=_TOOLS)

    assert turn.tool_calls == []
    assert turn.text == "All pods are healthy."
    assert turn.stop == "end"


def test_refusal_raises():
    p = _provider()
    fake_response = SimpleNamespace(
        stop_reason="refusal",
        content=[SimpleNamespace(type="text", text="no")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0),
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response
    with patch.object(p, "_get_client", return_value=fake_client):
        with pytest.raises(LLMProviderError, match="refusal"):
            p.generate_with_tools(messages=[{"role": "user", "content": "x"}], tools=_TOOLS)


def test_disabled_provider_raises():
    p = AnthropicProvider(api_key="", model="claude-opus-4-8")
    with pytest.raises(LLMProviderError, match="ANTHROPIC_API_KEY"):
        p.generate_with_tools(messages=[{"role": "user", "content": "x"}], tools=_TOOLS)


def test_message_mapping_round_trips_assistant_and_tool_roles():
    p = _provider()
    fake_response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="done")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0),
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response
    messages = [
        {"role": "user", "content": "find failing pods"},
        {
            "role": "assistant",
            "content": "checking",
            "tool_calls": [ToolCall(id="toolu_1", name="get_pods", arguments={"namespace": "x"})],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "pod a: CrashLoopBackOff"},
    ]
    with patch.object(p, "_get_client", return_value=fake_client):
        p.generate_with_tools(messages=messages, tools=_TOOLS)

    sent = fake_client.messages.create.call_args.kwargs["messages"]
    assert sent[0] == {"role": "user", "content": "find failing pods"}
    assert sent[1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "checking"},
            {"type": "tool_use", "id": "toolu_1", "name": "get_pods", "input": {"namespace": "x"}},
        ],
    }
    assert sent[2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "pod a: CrashLoopBackOff"}
        ],
    }


def test_parallel_tool_results_coalesce_into_one_user_message():
    # Two consecutive neutral tool messages (parallel calls) must map to ONE
    # Anthropic user message with two tool_result blocks — the API requires
    # strict user/assistant alternation.
    p = _provider()
    fake_response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="done")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0),
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response
    messages = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "parallel",
            "tool_calls": [
                ToolCall(id="c1", name="get_pods", arguments={}),
                ToolCall(id="c2", name="get_events", arguments={}),
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "get_pods", "content": "podA"},
        {"role": "tool", "tool_call_id": "c2", "name": "get_events", "content": "evtB"},
    ]
    with patch.object(p, "_get_client", return_value=fake_client):
        p.generate_with_tools(messages=messages, tools=_TOOLS)

    sent = fake_client.messages.create.call_args.kwargs["messages"]
    # user(q), assistant(2 tool_use), user(2 tool_result) — 3 messages, alternating.
    assert [msg["role"] for msg in sent] == ["user", "assistant", "user"]
    result_blocks = sent[2]["content"]
    assert [b["tool_use_id"] for b in result_blocks] == ["c1", "c2"]
    assert all(b["type"] == "tool_result" for b in result_blocks)


def test_no_tools_omits_tools_kwarg():
    p = _provider()
    fake_response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="hi")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0),
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response
    with patch.object(p, "_get_client", return_value=fake_client):
        p.generate_with_tools(messages=[{"role": "user", "content": "x"}], tools=[])
    assert "tools" not in fake_client.messages.create.call_args.kwargs
