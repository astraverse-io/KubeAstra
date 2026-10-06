"""OpenAI native tool-calling (generate_with_tools) — mocked httpx.

Patches services.llm.openai_provider.httpx.post (same convention as the deadline
test) with a fake that captures the request payload and returns a canned Chat
Completions body. Covers tool_calls parsing (JSON-string arguments), direct
answer, malformed arguments, stop normalization, usage, the tools payload shape,
and the provider-neutral -> OpenAI message mapping.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.llm.base import LLMProviderError, ToolCall  # noqa: E402
from services.llm.openai_provider import OpenAIProvider  # noqa: E402

_TOOLS = [
    {
        "name": "get_pods",
        "description": "List pods",
        "input_schema": {"type": "object", "properties": {"namespace": {"type": "string"}}},
    }
]


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def _patch_post(monkeypatch, body: dict, captured: dict):
    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return _FakeResponse(body)

    monkeypatch.setattr("services.llm.openai_provider.httpx.post", fake_post)


def _provider() -> OpenAIProvider:
    return OpenAIProvider(api_key="sk-x", model="gpt-4o")


def test_supports_native_tools_true():
    assert _provider().supports_native_tools() is True


def test_parses_tool_calls(monkeypatch):
    body = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_pods",
                                "arguments": json.dumps({"namespace": "kube-system"}),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 40, "completion_tokens": 8},
    }
    captured: dict = {}
    _patch_post(monkeypatch, body, captured)
    turn = _provider().generate_with_tools(
        messages=[{"role": "user", "content": "failing pods?"}], tools=_TOOLS, system="terse"
    )

    assert turn.stop == "tool_calls"
    assert turn.text == ""
    assert len(turn.tool_calls) == 1
    call = turn.tool_calls[0]
    assert (call.id, call.name, call.arguments) == ("call_1", "get_pods", {"namespace": "kube-system"})
    assert turn.usage.tokens_in == 40 and turn.usage.tokens_out == 8

    payload = captured["json"]
    assert payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_pods",
                "description": "List pods",
                "parameters": _TOOLS[0]["input_schema"],
            },
        }
    ]
    assert payload["messages"][0] == {"role": "system", "content": "terse"}
    assert payload["messages"][1] == {"role": "user", "content": "failing pods?"}


def test_direct_answer_has_no_tool_calls(monkeypatch):
    body = {
        "choices": [{"finish_reason": "stop", "message": {"content": "All healthy."}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }
    _patch_post(monkeypatch, body, {})
    turn = _provider().generate_with_tools(messages=[{"role": "user", "content": "status?"}], tools=_TOOLS)
    assert turn.tool_calls == []
    assert turn.text == "All healthy."
    assert turn.stop == "end"


def test_malformed_arguments_degrade_to_empty(monkeypatch):
    body = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "get_pods", "arguments": "{not json"}}
                    ],
                },
            }
        ],
        "usage": {},
    }
    _patch_post(monkeypatch, body, {})
    turn = _provider().generate_with_tools(messages=[{"role": "user", "content": "x"}], tools=_TOOLS)
    assert turn.tool_calls[0].arguments == {}


def test_disabled_provider_raises():
    p = OpenAIProvider(api_key="", model="gpt-4o")
    with pytest.raises(LLMProviderError, match="OPENAI_API_KEY"):
        p.generate_with_tools(messages=[{"role": "user", "content": "x"}], tools=_TOOLS)


def test_message_mapping_round_trips_assistant_and_tool_roles(monkeypatch):
    body = {
        "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
        "usage": {},
    }
    captured: dict = {}
    _patch_post(monkeypatch, body, captured)
    messages = [
        {"role": "user", "content": "find failing pods"},
        {
            "role": "assistant",
            "content": "checking",
            "tool_calls": [ToolCall(id="c1", name="get_pods", arguments={"namespace": "x"})],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "pod a: CrashLoopBackOff"},
    ]
    _provider().generate_with_tools(messages=messages, tools=_TOOLS)

    sent = captured["json"]["messages"]
    assert sent[0] == {"role": "user", "content": "find failing pods"}
    assert sent[1] == {
        "role": "assistant",
        "content": "checking",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "get_pods", "arguments": json.dumps({"namespace": "x"})},
            }
        ],
    }
    assert sent[2] == {"role": "tool", "tool_call_id": "c1", "content": "pod a: CrashLoopBackOff"}


def test_no_tools_omits_tools_key(monkeypatch):
    body = {"choices": [{"finish_reason": "stop", "message": {"content": "hi"}}], "usage": {}}
    captured: dict = {}
    _patch_post(monkeypatch, body, captured)
    _provider().generate_with_tools(messages=[{"role": "user", "content": "x"}], tools=[])
    assert "tools" not in captured["json"]
