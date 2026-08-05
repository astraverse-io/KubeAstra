"""Embeddings backend selection and degradation.

The load-bearing behaviours:
  - the module-level `embeddings` singleton keeps working (8 call sites import
    it directly, so replacing it with a factory would be a breaking change);
  - a missing embeddings key degrades to keyword-only instead of failing an
    investigation;
  - transient upstream errors retry, configuration errors do not.
"""

import importlib

import pytest

from services import embeddings as module


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """Each test gets a fresh dispatcher and no ambient provider keys."""
    for name in (
        "VOYAGE_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
        "GOOGLE_API_KEY", "EMBEDDINGS_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    module.embeddings.reset()
    yield
    module.embeddings.reset()


def _configure(monkeypatch, **values):
    for key, value in values.items():
        monkeypatch.setattr(module.settings, key, value, raising=False)
    module.embeddings.reset()


# ── the singleton contract ────────────────────────────────────────────────


def test_singleton_import_surface_is_preserved():
    """Eight call sites do exactly this import. It must not regress."""
    from services.embeddings import embeddings

    assert hasattr(embeddings, "embed")
    assert hasattr(embeddings, "embed_many")


def test_singleton_survives_reload():
    reloaded = importlib.reload(module)
    assert hasattr(reloaded, "embeddings")


# ── backend selection ─────────────────────────────────────────────────────


def test_api_mode_without_key_degrades_to_null(monkeypatch):
    _configure(monkeypatch, embeddings_mode="api", embeddings_provider="voyage")
    assert module.embeddings.available is False


def test_api_mode_with_key_selects_provider_backend(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "vk-test")
    _configure(monkeypatch, embeddings_mode="api", embeddings_provider="voyage")
    assert module.embeddings.available is True
    assert module.embeddings.dim == 1024


def test_provider_without_embeddings_api_degrades(monkeypatch):
    """Anthropic has no embeddings endpoint — decision 1's whole premise."""
    _configure(monkeypatch, embeddings_mode="api", embeddings_provider="anthropic")
    assert module.embeddings.available is False


def test_available_is_false_when_local_deps_absent(monkeypatch):
    """Regression: constructing _LocalBackend does not import
    sentence-transformers, so `available` must not assume it is installed —
    desktop bundles exclude torch on purpose. Reporting True here would make
    the UI claim memory works right before the first embed raises.
    """
    _configure(monkeypatch, embeddings_mode="local")
    import importlib.util

    real_find_spec = importlib.util.find_spec

    def missing(name, *args, **kwargs):
        if name == "sentence_transformers":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", missing)
    assert module.embeddings.available is False


def test_ollama_mode_needs_no_key(monkeypatch):
    """No key, and no `_NullBackend` degradation for the want of one."""
    _configure(monkeypatch, embeddings_mode="ollama")
    assert isinstance(module.embeddings._resolve(), module._OllamaBackend)
    assert module.embeddings.dim == 768


def test_ollama_availability_follows_the_daemon(monkeypatch):
    """`available` used to be unconditionally True here.

    It was harmless while nothing selected this backend: desktop mode landed
    on `_NullBackend`, which reports False honestly. Wiring Ollama up made the
    unconditional True a claim that a running daemon with the embedding model
    pulled exists, which is two assumptions and neither is checked by having
    picked Ollama for chat.
    """
    _configure(monkeypatch, embeddings_mode="ollama")

    monkeypatch.setattr(
        module, "_get_json", lambda url, timeout: {"models": [{"name": "llama3"}]}
    )
    assert module.embeddings.available is False

    monkeypatch.setattr(
        module,
        "_get_json",
        lambda url, timeout: {"models": [{"name": "nomic-embed-text:latest"}]},
    )
    assert module.embeddings.available is True


def test_ollama_is_unavailable_when_nothing_answers(monkeypatch):
    """A probe that raises is a "no", not a crash on the settings screen."""
    _configure(monkeypatch, embeddings_mode="ollama")

    def refuse(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(module, "_get_json", refuse)
    assert module.embeddings.available is False


@pytest.mark.parametrize(
    "provider,dim",
    [("voyage", 1024), ("openai", 1536), ("gemini", 768)],
)
def test_provider_dimensions(monkeypatch, provider, dim):
    """Dimensions are written into the vector collection; a wrong value
    corrupts recall silently rather than erroring."""
    monkeypatch.setenv("EMBEDDINGS_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "k")
    _configure(monkeypatch, embeddings_mode="api", embeddings_provider=provider)
    assert module.embeddings.dim == dim


def test_reset_reresolves_after_key_added(monkeypatch):
    _configure(monkeypatch, embeddings_mode="api", embeddings_provider="voyage")
    assert module.embeddings.available is False

    monkeypatch.setenv("VOYAGE_API_KEY", "vk-added-later")
    module.embeddings.reset()
    assert module.embeddings.available is True


# ── degradation ───────────────────────────────────────────────────────────


def test_null_backend_raises_typed_error(monkeypatch):
    _configure(monkeypatch, embeddings_mode="api", embeddings_provider="voyage")
    with pytest.raises(module.EmbeddingsUnavailable):
        module.embeddings.embed("anything")


def test_null_backend_message_is_actionable(monkeypatch):
    _configure(monkeypatch, embeddings_mode="api", embeddings_provider="voyage")
    with pytest.raises(module.EmbeddingsUnavailable) as excinfo:
        module.embeddings.embed_many(["x"])
    message = str(excinfo.value).lower()
    assert "keyword" in message and "settings" in message


def test_empty_input_short_circuits(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "vk")
    _configure(monkeypatch, embeddings_mode="api", embeddings_provider="voyage")
    assert module.embeddings.embed_many([]) == []


# ── retry policy ──────────────────────────────────────────────────────────


def test_retryable_error_is_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    attempts = {"n": 0}

    class Flaky(module._HttpBackend):
        provider = "test"

        def _request(self, texts):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise module._RetryableHttpError("HTTP 429")
            return [[0.1] * 4 for _ in texts]

    backend = Flaky("m", 4, 1.0)
    assert backend.embed_many(["a"]) == [[0.1] * 4]
    assert attempts["n"] == 3


def test_retries_exhausted_becomes_unavailable(monkeypatch):
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    class AlwaysDown(module._HttpBackend):
        provider = "test"

        def _request(self, texts):
            raise module._RetryableHttpError("HTTP 503")

    with pytest.raises(module.EmbeddingsUnavailable):
        AlwaysDown("m", 4, 1.0).embed_many(["a"])


def test_configuration_error_is_not_retried(monkeypatch):
    """A 401 will never succeed on retry; failing fast surfaces the real
    problem instead of a timeout three attempts later."""
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    attempts = {"n": 0}

    class BadKey(module._HttpBackend):
        provider = "test"

        def _request(self, texts):
            attempts["n"] += 1
            raise RuntimeError("HTTP 401: invalid api key")

    with pytest.raises(module.EmbeddingsUnavailable):
        BadKey("m", 4, 1.0).embed_many(["a"])
    assert attempts["n"] == 1


def test_embed_delegates_to_embed_many(monkeypatch):
    class Single(module._HttpBackend):
        provider = "test"

        def _request(self, texts):
            return [[float(len(t))] for t in texts]

    assert Single("m", 1, 1.0).embed("abcd") == [4.0]
