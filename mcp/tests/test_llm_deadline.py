"""Request-local LLM deadline enforcement."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from services.llm import base
from services.llm.base import (
    LLMProviderError,
    effective_timeout,
    reset_generation_deadline,
    set_generation_deadline,
)
from services.llm.gemini_provider import GeminiProvider
from services.llm.ollama_provider import OllamaProvider


def test_effective_timeout_is_request_local_and_resets(monkeypatch):
    monkeypatch.setattr(base.time, "monotonic", lambda: 100.0)
    assert effective_timeout(60) == 60

    token = set_generation_deadline(105.0)
    try:
        assert effective_timeout(60) == 5
    finally:
        reset_generation_deadline(token)

    assert effective_timeout(60) == 60


def test_effective_timeout_rejects_expired_deadline(monkeypatch):
    monkeypatch.setattr(base.time, "monotonic", lambda: 100.0)
    token = set_generation_deadline(99.0)
    try:
        with pytest.raises(LLMProviderError, match="deadline expired"):
            effective_timeout(60)
    finally:
        reset_generation_deadline(token)


def test_ollama_uses_remaining_worker_budget(monkeypatch):
    monkeypatch.setattr(base.time, "monotonic", lambda: 20.0)
    observed = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": "ok"}}

    def fake_post(url, **kwargs):
        observed["timeout"] = kwargs["timeout"]
        return _Response()

    monkeypatch.setattr("services.llm.ollama_provider.httpx.post", fake_post)
    provider = OllamaProvider(
        base_url="http://ollama.internal",
        model="test",
        timeout=120,
    )
    token = set_generation_deadline(23.5)
    try:
        assert provider.generate("hello") == "ok"
    finally:
        reset_generation_deadline(token)
    assert observed["timeout"] == 3.5


def test_gemini_builds_deadline_scoped_client_without_replacing_cache(monkeypatch):
    monkeypatch.setattr(base.time, "monotonic", lambda: 40.0)
    observed = []

    class _Types:
        @staticmethod
        def HttpOptions(*, timeout):
            observed.append(timeout)
            return SimpleNamespace(timeout=timeout)

    class _GenAI:
        @staticmethod
        def Client(**kwargs):
            return SimpleNamespace(kwargs=kwargs)

    google = ModuleType("google")
    genai = ModuleType("google.genai")
    genai.Client = _GenAI.Client
    genai.types = _Types
    google.genai = genai
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", _Types)

    provider = GeminiProvider(api_key="secret", model="test", timeout=60)
    cached = SimpleNamespace(name="cached")
    provider._client = cached  # noqa: SLF001
    token = set_generation_deadline(42.0)
    try:
        scoped = provider._get_client(effective_timeout(60))  # noqa: SLF001
    finally:
        reset_generation_deadline(token)

    assert scoped is not cached
    assert provider._client is cached  # noqa: SLF001
    assert observed == [2000.0]
