"""Regression tests for RAG runtime log-noise controls."""

from pathlib import Path
import importlib
import logging
import sys

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))


def test_embedding_loader_sets_hf_quiet_defaults(monkeypatch):
    for key in (
        "HF_HUB_DISABLE_PROGRESS_BARS",
        "HF_HUB_VERBOSITY",
        "TRANSFORMERS_VERBOSITY",
        "TOKENIZERS_PARALLELISM",
    ):
        monkeypatch.delenv(key, raising=False)

    import services.embeddings as embeddings_module

    module = importlib.reload(embeddings_module)

    class FakeSentenceTransformer:
        def __init__(self, model_name):
            self.model_name = model_name

        def encode(self, text, normalize_embeddings=True):
            class FakeVector(list):
                def tolist(self):
                    return list(self)

            return FakeVector([0.1, 0.2])

    monkeypatch.setitem(sys.modules, "sentence_transformers", type(
        "FakeSentenceTransformersModule",
        (),
        {"SentenceTransformer": FakeSentenceTransformer},
    ))

    service = module.EmbeddingService()
    service.embed("hello")

    assert module.os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] == "1"
    assert module.os.environ["HF_HUB_VERBOSITY"] == "error"
    assert module.os.environ["TRANSFORMERS_VERBOSITY"] == "error"
    assert module.os.environ["TOKENIZERS_PARALLELISM"] == "false"
    assert logging.getLogger("huggingface_hub").level == logging.ERROR
    assert logging.getLogger("transformers").level == logging.ERROR


def test_vector_search_can_suppress_router_warning(caplog):
    from services.vector_db import VectorDB

    class BrokenClient:
        def query_points(self, **kwargs):
            raise ConnectionError("connection refused")

    db = VectorDB()
    db._client = BrokenClient()

    with caplog.at_level(logging.WARNING):
        assert db.search_in("runbook", [0.1], log_failures=False) == []

    assert "Vector search in runbook failed" not in caplog.text
