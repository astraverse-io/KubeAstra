"""Anthropic + OpenAI provider tests.

Exercises the raw HTTP / SDK surface with mocks so we don't need real API
keys in CI. Covers: enabled/disabled gating, happy-path text extraction,
usage extraction into TokenUsage, error mapping, and factory dispatch.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.llm.anthropic_provider import AnthropicProvider  # noqa: E402
from services.llm.base import LLMProviderError  # noqa: E402
from services.llm.openai_provider import OpenAIProvider  # noqa: E402


# ── Anthropic ─────────────────────────────────────────────────────────────────


def test_anthropic_disabled_without_key():
    p = AnthropicProvider(api_key="", model="claude-opus-4-8")
    assert p.enabled is False
    with pytest.raises(LLMProviderError, match="ANTHROPIC_API_KEY"):
        p.generate("hi")


def test_anthropic_disabled_without_model():
    p = AnthropicProvider(api_key="sk-ant-x", model="")
    assert p.enabled is False


def test_anthropic_enabled_when_configured():
    p = AnthropicProvider(api_key="sk-ant-x", model="claude-opus-4-8")
    assert p.enabled is True


def test_anthropic_generate_extracts_text_from_blocks():
    p = AnthropicProvider(api_key="sk-ant-x", model="claude-opus-4-8")
    fake_response = SimpleNamespace(
        stop_reason="end_turn",
        content=[
            SimpleNamespace(type="text", text="Hello, "),
            SimpleNamespace(type="text", text="world."),
        ],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0),
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response
    with patch.object(p, "_get_client", return_value=fake_client):
        assert p.generate("prompt", system="sys") == "Hello, world."
    # System + user routing verified by kwargs sent to the SDK.
    kwargs = fake_client.messages.create.call_args.kwargs
    assert kwargs["system"] == "sys"
    assert kwargs["messages"] == [{"role": "user", "content": "prompt"}]


def test_anthropic_refusal_raises():
    p = AnthropicProvider(api_key="sk-ant-x", model="claude-opus-4-8")
    fake_response = SimpleNamespace(
        stop_reason="refusal",
        content=[SimpleNamespace(type="text", text="I can't help with that.")],
        usage=SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0),
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response
    with patch.object(p, "_get_client", return_value=fake_client):
        with pytest.raises(LLMProviderError, match="refusal"):
            p.generate("prompt")


def test_anthropic_empty_response_raises():
    p = AnthropicProvider(api_key="sk-ant-x", model="claude-opus-4-8")
    fake_response = SimpleNamespace(
        stop_reason="end_turn",
        content=[],  # no text blocks
        usage=SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0),
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response
    with patch.object(p, "_get_client", return_value=fake_client):
        with pytest.raises(LLMProviderError, match="empty"):
            p.generate("prompt")


def test_anthropic_generate_with_usage_returns_token_counts():
    p = AnthropicProvider(api_key="sk-ant-x", model="claude-opus-4-8")
    fake_response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="ok")],
        usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=30),
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response
    with patch.object(p, "_get_client", return_value=fake_client):
        text, usage = p.generate_with_usage("prompt")
    assert text == "ok"
    assert usage.tokens_in == 100
    assert usage.cached_tokens_in == 30
    assert usage.tokens_out == 20
    assert usage.model == "claude-opus-4-8"
    assert usage.cost_usd > 0  # priced model


# ── OpenAI ────────────────────────────────────────────────────────────────────


def test_openai_disabled_without_key():
    p = OpenAIProvider(api_key="", model="gpt-4o")
    assert p.enabled is False
    with pytest.raises(LLMProviderError, match="OPENAI_API_KEY"):
        p.generate("hi")


def test_openai_enabled_when_configured():
    p = OpenAIProvider(api_key="sk-x", model="gpt-4o")
    assert p.enabled is True


def test_openai_base_url_strips_trailing_slash():
    p = OpenAIProvider(api_key="sk-x", model="gpt-4o", base_url="https://api.openai.com/v1/")
    assert p._base_url == "https://api.openai.com/v1"


def test_openai_generate_extracts_content():
    p = OpenAIProvider(api_key="sk-x", model="gpt-4o")
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "choices": [{"message": {"content": "Hello, world."}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    with patch("httpx.post", return_value=fake_response) as fake_post:
        assert p.generate("prompt", system="sys") == "Hello, world."
    args, kwargs = fake_post.call_args
    assert args[0] == "https://api.openai.com/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer sk-x"
    payload = kwargs["json"]
    assert payload["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "prompt"},
    ]


def test_openai_empty_response_raises():
    p = OpenAIProvider(api_key="sk-x", model="gpt-4o")
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {"choices": [{"message": {"content": ""}}]}
    with patch("httpx.post", return_value=fake_response):
        with pytest.raises(LLMProviderError, match="empty"):
            p.generate("prompt")


def test_openai_http_error_wraps_into_provider_error():
    import httpx

    p = OpenAIProvider(api_key="sk-x", model="gpt-4o")
    with patch("httpx.post", side_effect=httpx.HTTPError("boom")):
        with pytest.raises(LLMProviderError, match="OpenAI request failed"):
            p.generate("prompt")


def test_openai_generate_with_usage_extracts_cached_tokens():
    p = OpenAIProvider(api_key="sk-x", model="gpt-4o")
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 40},
        },
    }
    with patch("httpx.post", return_value=fake_response):
        text, usage = p.generate_with_usage("prompt")
    assert text == "ok"
    assert usage.tokens_in == 100
    assert usage.cached_tokens_in == 40
    assert usage.tokens_out == 20
    assert usage.model == "gpt-4o"
    assert usage.cost_usd > 0


def test_openai_max_tokens_uses_completion_key():
    """OpenAI's newer API uses max_completion_tokens, not max_tokens."""
    p = OpenAIProvider(api_key="sk-x", model="gpt-4o")
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {},
    }
    with patch("httpx.post", return_value=fake_response) as fake_post:
        p.generate("prompt", max_tokens=500)
    payload = fake_post.call_args.kwargs["json"]
    assert payload["max_completion_tokens"] == 500
    assert "max_tokens" not in payload


# ── Factory dispatch ──────────────────────────────────────────────────────────


def test_factory_dispatches_to_anthropic(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-4-8")
    from config.settings import get_settings
    from services.llm import get_provider

    get_settings.cache_clear()
    provider = get_provider()
    assert provider.name == "anthropic"
    assert provider.enabled is True


def test_factory_dispatches_to_openai(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    from config.settings import get_settings
    from services.llm import get_provider

    get_settings.cache_clear()
    provider = get_provider()
    assert provider.name == "openai"
    assert provider.enabled is True


def test_factory_rejects_unknown_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bogus")
    from config.settings import get_settings
    from services.llm import get_provider

    get_settings.cache_clear()
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        get_provider()
