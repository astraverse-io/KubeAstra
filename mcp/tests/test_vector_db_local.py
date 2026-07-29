"""Local (embedded) vector store mode and the embedding-dimension guard.

The dimension guard exists because switching embeddings provider changes the
vector width (Voyage 1024 / OpenAI 1536 / Gemini and Ollama 768 / MiniLM 384).
Without the check, recall quietly stops working instead of reporting why.
"""

from types import SimpleNamespace

import pytest

from services import vector_db as module


@pytest.fixture
def db(monkeypatch):
    instance = module.VectorDB()
    instance._vector_size = 384
    instance.collection = "k8s_errors"
    return instance


def _collection_info(size):
    """Mimic qdrant_client's nested config object."""
    return SimpleNamespace(
        config=SimpleNamespace(params=SimpleNamespace(vectors=SimpleNamespace(size=size)))
    )


# ── reading the stored dimension ──────────────────────────────────────────


def test_reads_size_from_collection_config(db):
    db._client = SimpleNamespace(get_collection=lambda name: _collection_info(768))
    assert db._stored_vector_size("k8s_errors") == 768


def test_reads_size_from_named_vectors(db):
    """Named-vector collections expose a mapping rather than a single object."""
    vectors = {"default": SimpleNamespace(size=1024)}
    info = SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors=vectors)))
    db._client = SimpleNamespace(get_collection=lambda name: info)
    assert db._stored_vector_size("k8s_errors") == 1024


def test_unreadable_config_returns_none_rather_than_guessing(db):
    def boom(name):
        raise RuntimeError("connection lost")

    db._client = SimpleNamespace(get_collection=boom)
    assert db._stored_vector_size("k8s_errors") is None


def test_unfamiliar_shape_returns_none(db):
    info = SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors=None)))
    db._client = SimpleNamespace(get_collection=lambda name: info)
    assert db._stored_vector_size("k8s_errors") is None


# ── the guard ─────────────────────────────────────────────────────────────


def test_matching_dimension_passes(db):
    db._client = SimpleNamespace(get_collection=lambda name: _collection_info(384))
    db._assert_dimension_matches("k8s_errors")  # must not raise


def test_mismatch_raises(db):
    """The scenario: user switched Anthropic+MiniLM -> Voyage."""
    db._client = SimpleNamespace(get_collection=lambda name: _collection_info(1024))
    with pytest.raises(module.EmbeddingDimensionMismatch) as excinfo:
        db._assert_dimension_matches("k8s_errors")
    error = excinfo.value
    assert error.stored_dim == 1024
    assert error.configured_dim == 384


def test_mismatch_message_tells_the_user_what_to_do(db):
    db._client = SimpleNamespace(get_collection=lambda name: _collection_info(1536))
    with pytest.raises(module.EmbeddingDimensionMismatch) as excinfo:
        db._assert_dimension_matches("k8s_errors")
    message = str(excinfo.value).lower()
    assert "1536" in message and "384" in message
    assert "clear" in message or "restore" in message


def test_unknown_stored_size_does_not_block(db):
    """A read failure must not stop the app starting — false positives here
    would be worse than the mismatch we are guarding against."""
    db._client = SimpleNamespace(
        get_collection=lambda name: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    db._assert_dimension_matches("k8s_errors")  # must not raise


def test_explicit_expected_overrides_default(db):
    db._client = SimpleNamespace(get_collection=lambda name: _collection_info(768))
    db._assert_dimension_matches("other", expected=768)
    with pytest.raises(module.EmbeddingDimensionMismatch):
        db._assert_dimension_matches("other", expected=384)


# ── local mode config ─────────────────────────────────────────────────────


def test_local_mode_requires_a_path(monkeypatch, db):
    monkeypatch.setattr(module.settings, "vector_db_mode", "local", raising=False)
    monkeypatch.setattr(module.settings, "vector_db_path", "", raising=False)
    with pytest.raises(RuntimeError, match="VECTOR_DB_PATH"):
        db.connect()


def test_is_local_reads_live_config(monkeypatch, db):
    """Regression: _is_local was cached in __init__, but this module ends with
    a module-level singleton, so the mode was frozen at import."""
    monkeypatch.setattr(module.settings, "vector_db_mode", "server", raising=False)
    assert db._is_local is False
    monkeypatch.setattr(module.settings, "vector_db_mode", "local", raising=False)
    assert db._is_local is True


def test_locked_store_error_names_the_path():
    error = module.VectorStoreLockedError("/tmp/vectors")
    assert "/tmp/vectors" in str(error)
    assert error.path == "/tmp/vectors"
