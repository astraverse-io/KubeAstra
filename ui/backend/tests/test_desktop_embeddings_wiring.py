"""Which embeddings backend the wizard's choices actually produce.

The wizard asks for an embeddings key only when the chat provider is
Anthropic, on the reasoning that the other three either are an embeddings
provider or run locally. That reasoning was never wired up: `_apply_provider`
set `LLM_PROVIDER` and stopped, leaving `EMBEDDINGS_PROVIDER` empty. With
`EMBEDDINGS_MODE=api` — the desktop default from `desktop_main` — an empty
provider takes the `_NullBackend` branch in `services/embeddings.py`, and
investigation memory silently drops to keyword-only.

So the only provider that ended up with working vector memory was the one
with no embeddings API at all, because it was the only one the wizard asked
about. These tests pin the resolved backend for each choice, rather than the
environment variables, because the environment is the means and the backend
is the thing the user gets.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import desktop_secrets  # noqa: E402
from config.settings import Settings, get_settings  # noqa: E402
from routers import desktop as desktop_router  # noqa: E402


class _MemoryKeyring:
    __module__ = "keyring.backends.macOS"

    def __init__(self):
        self.store = {}

    def get_keyring(self):
        return self

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def get_password(self, service, name):
        return self.store.get((service, name))

    def delete_password(self, service, name):
        self.store.pop((service, name), None)


@pytest.fixture
def wizard(tmp_path, monkeypatch):
    """`_apply_provider` for real, with the keychain and caches faked out.

    Every environment variable the function under test writes is registered
    with monkeypatch first, including ones this fixture does not otherwise
    care about: monkeypatch restores what it recorded, so registering
    `LLM_PROVIDER` here is what stops `_apply_provider` leaving `ollama`
    behind for whichever test runs next.
    """
    monkeypatch.setenv("KUBEASTRA_STATE_DIR", str(tmp_path))
    desktop_secrets.clear_cache()
    monkeypatch.setattr(desktop_secrets, "_keyring", lambda: _MemoryKeyring())
    monkeypatch.setattr(desktop_router, "_reset_caches", lambda: None)
    # The desktop default, set by desktop_main before anything else runs.
    monkeypatch.setenv("EMBEDDINGS_MODE", "api")
    for name in (
        "LLM_PROVIDER",
        "EMBEDDINGS_PROVIDER",
        "EMBEDDINGS_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    yield desktop_router

    # Teardown runs *before* monkeypatch restores the environment — fixture
    # finalizers unwind in reverse, and this fixture requested monkeypatch, so
    # it was set up first and is torn down last. Rebuilding settings here
    # would therefore cache exactly the values the test was about to discard:
    # `get_settings` is `lru_cache`d, so one call with `LLM_PROVIDER=ollama`
    # still in the environment makes every later test see an Ollama install.
    #
    # Clearing the cache leaves nothing behind and needs no ordering
    # guarantee: whoever calls next rebuilds against whatever the environment
    # is by then.
    desktop_secrets.clear_cache()
    get_settings.cache_clear()


def _resolved_backend():
    """The backend class `services.embeddings` would build right now.

    `services.embeddings` reads a module-level `settings` captured at import,
    so a fresh instance has to be swapped in for `_build` to see what the
    wizard just wrote. Done with `patch.object` rather than by reloading the
    module or evicting it from `sys.modules`: other test files bind
    `get_settings` and `settings` at *their* import time, and replacing the
    module object leaves them holding a different `lru_cache` than the one
    they clear — which silently breaks tests nowhere near this file.
    """
    import services.embeddings as embeddings_module

    fresh = Settings()  # reads os.environ as it stands
    with patch.object(embeddings_module, "settings", fresh):
        return type(embeddings_module.embeddings._build()).__name__


@pytest.mark.parametrize(
    "provider, expected",
    [
        ("openai", "_OpenAIBackend"),
        ("gemini", "_GeminiBackend"),
    ],
)
def test_provider_with_an_embeddings_api_gets_vector_memory(
    wizard, provider, expected
):
    """The wizard does not ask these two for an embeddings key. It must
    therefore reuse the chat key it already has, or memory is keyword-only
    and nothing says so."""
    desktop_secrets.set_secret(f"llm.{provider}", "sk-chat-key")

    wizard._apply_provider(provider)

    assert _resolved_backend() == expected


def test_ollama_embeds_through_the_local_daemon(wizard):
    """Choosing Ollama is a request for nothing to leave the machine.

    `api` mode with provider `ollama` is not a configuration that exists —
    `_build` rejects it and returns `_NullBackend`. The local daemon is the
    only backend consistent with the choice the user made.
    """
    wizard._apply_provider("ollama")

    assert _resolved_backend() == "_OllamaBackend"


def test_anthropic_still_defers_to_the_separate_embeddings_key(wizard):
    """Anthropic publishes no embeddings API, so there is nothing to reuse.

    This is the path that already worked; it must keep working.
    """
    desktop_secrets.set_secret("llm.anthropic", "sk-ant-chat")

    wizard._apply_provider("anthropic")

    assert _resolved_backend() == "_NullBackend"

    desktop_secrets.set_secret("embeddings.voyage", "vk-embed")
    wizard._apply_embeddings("voyage")

    assert _resolved_backend() == "_VoyageBackend"


def test_explicit_embeddings_choice_outranks_the_chat_provider(wizard):
    """A user who picks Voyage while chatting with OpenAI meant it."""
    desktop_secrets.set_secret("llm.openai", "sk-chat-key")
    desktop_secrets.set_secret("embeddings.voyage", "vk-embed")

    wizard._apply_provider("openai")
    wizard._apply_embeddings("voyage")

    assert _resolved_backend() == "_VoyageBackend"


def test_switching_to_ollama_stops_sending_text_to_a_hosted_provider(wizard):
    """The privacy-relevant direction of the switch.

    Someone who configured Voyage and then moves the chat model to Ollama is
    asking for a local setup. Continuing to POST investigation text to Voyage
    would be a quieter failure than keyword-only memory, and a worse one.
    """
    desktop_secrets.set_secret("embeddings.voyage", "vk-embed")
    wizard._apply_embeddings("voyage")
    assert _resolved_backend() == "_VoyageBackend"

    wizard._apply_provider("ollama")

    assert _resolved_backend() == "_OllamaBackend"


# ── reporting memory status honestly ──────────────────────────────────────
#
# `EmbeddingService.available` promises not to "claim availability the first
# embed would refuse". It kept that promise by accident for Ollama, because
# desktop mode never reached the Ollama backend — `_NullBackend` reported
# False and the UI said "keyword". Wiring Ollama up turns the accident into a
# lie: the daemon may not be running, or may not have the embedding model
# pulled, and nothing above this asks.


def _ollama_backend():
    import services.embeddings as embeddings_module

    return embeddings_module._OllamaBackend("nomic-embed-text", 768, 5.0)


def test_ollama_is_unavailable_when_the_daemon_is_down(wizard, monkeypatch):
    import services.embeddings as embeddings_module

    def refuse(url, timeout):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(embeddings_module, "_get_json", refuse)

    assert _ollama_backend().ready() is False


def test_ollama_is_unavailable_when_the_model_was_never_pulled(wizard, monkeypatch):
    """`ollama serve` running is not the same as the model being present."""
    import services.embeddings as embeddings_module

    monkeypatch.setattr(
        embeddings_module,
        "_get_json",
        lambda url, timeout: {"models": [{"name": "llama3:latest"}]},
    )

    assert _ollama_backend().ready() is False


def test_ollama_is_available_once_the_model_is_pulled(wizard, monkeypatch):
    """Ollama reports `nomic-embed-text:latest` for a bare `nomic-embed-text`."""
    import services.embeddings as embeddings_module

    monkeypatch.setattr(
        embeddings_module,
        "_get_json",
        lambda url, timeout: {
            "models": [{"name": "llama3:latest"}, {"name": "nomic-embed-text:latest"}]
        },
    )

    assert _ollama_backend().ready() is True


def test_setup_reports_keyword_memory_when_ollama_has_no_model(wizard, monkeypatch):
    """The status the wizard shows, not just the backend's own opinion."""
    import services.embeddings as embeddings_module

    monkeypatch.setattr(
        embeddings_module,
        "_get_json",
        lambda url, timeout: {"models": []},
    )
    wizard._apply_provider("ollama")

    fresh = Settings()
    with patch.object(embeddings_module, "settings", fresh):
        service = embeddings_module.EmbeddingService()
        assert service.available is False
