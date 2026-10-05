"""Gemini native tool-calling (generate_with_tools) — mocked client.

Patches _get_client to a MagicMock whose models.generate_content returns a canned
response. generate_with_tools builds REAL google.genai `types` objects, so these
tests also validate that the provider-neutral -> Gemini mapping produces valid
SDK structures. Covers function_call parsing, direct answer, safety refusal,
disabled provider, the tool-spec (parameters_json_schema) mapping, and the
contents mapping (incl. assistant function_call + tool function_response).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.llm.base import LLMProviderError, ToolCall  # noqa: E402
from services.llm.gemini_provider import GeminiProvider  # noqa: E402

_SCHEMA = {"type": "object", "properties": {"namespace": {"type": "string"}}}
_TOOLS = [{"name": "get_pods", "description": "List pods", "input_schema": _SCHEMA}]


def _provider() -> GeminiProvider:
    return GeminiProvider(api_key="secret", model="gemini-2.5-flash", timeout=60)


def _response(parts, finish="STOP"):
    return SimpleNamespace(
        candidates=[
            SimpleNamespace(
                finish_reason=SimpleNamespace(name=finish),
                content=SimpleNamespace(parts=parts),
            )
        ],
        usage_metadata=SimpleNamespace(
            prompt_token_count=30, candidates_token_count=9, cached_content_token_count=0
        ),
    )


def test_supports_native_tools_true():
    assert _provider().supports_native_tools() is True


def test_parses_function_call():
    p = _provider()
    parts = [
        SimpleNamespace(text="Checking pods.", function_call=None),
        SimpleNamespace(
            function_call=SimpleNamespace(id="fc1", name="get_pods", args={"namespace": "kube-system"})
        ),
    ]
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = _response(parts)
    with patch.object(p, "_get_client", return_value=fake_client):
        turn = p.generate_with_tools(
            messages=[{"role": "user", "content": "failing pods?"}], tools=_TOOLS, system="terse"
        )

    assert turn.stop == "tool_calls"
    assert turn.text == "Checking pods."
    assert len(turn.tool_calls) == 1
    call = turn.tool_calls[0]
    assert (call.name, call.arguments) == ("get_pods", {"namespace": "kube-system"})
    assert turn.usage.tokens_in == 30 and turn.usage.tokens_out == 9

    # Tool spec mapping: raw JSON schema passed straight through.
    config = fake_client.models.generate_content.call_args.kwargs["config"]
    fd = config.tools[0].function_declarations[0]
    assert fd.name == "get_pods"
    assert fd.parameters_json_schema == _SCHEMA


def test_direct_answer_has_no_tool_calls():
    p = _provider()
    parts = [SimpleNamespace(text="All healthy.", function_call=None)]
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = _response(parts, finish="STOP")
    with patch.object(p, "_get_client", return_value=fake_client):
        turn = p.generate_with_tools(messages=[{"role": "user", "content": "status?"}], tools=_TOOLS)
    assert turn.tool_calls == []
    assert turn.text == "All healthy."
    assert turn.stop == "end"


def test_safety_finish_with_no_output_raises():
    p = _provider()
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = _response([], finish="SAFETY")
    with patch.object(p, "_get_client", return_value=fake_client):
        with pytest.raises(LLMProviderError, match="declined"):
            p.generate_with_tools(messages=[{"role": "user", "content": "x"}], tools=_TOOLS)


def test_disabled_provider_raises():
    p = GeminiProvider(api_key="", model="gemini-2.5-flash")
    with pytest.raises(LLMProviderError, match="Gemini API key is not configured"):
        p.generate_with_tools(messages=[{"role": "user", "content": "x"}], tools=_TOOLS)


def test_contents_mapping_round_trips_assistant_and_tool_roles():
    p = _provider()
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = _response(
        [SimpleNamespace(text="done", function_call=None)]
    )
    messages = [
        {"role": "user", "content": "find failing pods"},
        {
            "role": "assistant",
            "content": "checking",
            "tool_calls": [ToolCall(id="fc1", name="get_pods", arguments={"namespace": "x"})],
        },
        {"role": "tool", "tool_call_id": "fc1", "name": "get_pods", "content": "pod a: CrashLoopBackOff"},
    ]
    with patch.object(p, "_get_client", return_value=fake_client):
        p.generate_with_tools(messages=messages, tools=_TOOLS)

    contents = fake_client.models.generate_content.call_args.kwargs["contents"]
    assert contents[0].role == "user"
    assert contents[0].parts[0].text == "find failing pods"

    assert contents[1].role == "model"
    assert contents[1].parts[0].text == "checking"
    fc = contents[1].parts[1].function_call
    assert fc.name == "get_pods" and dict(fc.args) == {"namespace": "x"}

    assert contents[2].role == "user"
    fr = contents[2].parts[0].function_response
    assert fr.name == "get_pods"
    assert dict(fr.response) == {"result": "pod a: CrashLoopBackOff"}


def test_parallel_tool_results_coalesce_into_one_content():
    # Two consecutive neutral tool messages (parallel calls) must map to ONE
    # Gemini user Content with two function_response parts, preserving
    # user/model alternation.
    p = _provider()
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = _response(
        [SimpleNamespace(text="done", function_call=None)]
    )
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

    contents = fake_client.models.generate_content.call_args.kwargs["contents"]
    assert [c.role for c in contents] == ["user", "model", "user"]
    result_parts = contents[2].parts
    assert [prt.function_response.name for prt in result_parts] == ["get_pods", "get_events"]
