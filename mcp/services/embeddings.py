"""Sentence-transformer embeddings for semantic error similarity search.

SentenceTransformer (and transitively torch) is imported lazily at first use,
not at module load time. This prevents startup crashes in environments where:
  - UID is not present in /etc/passwd (torch.getpwuid fails)
  - Weaviate/RAG is not configured and embeddings are never called
"""

import logging
import os
from typing import TYPE_CHECKING, Optional

from config.settings import get_settings

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer as _ST

logger = logging.getLogger(__name__)
settings = get_settings()

_HF_QUIET_DEFAULTS = {
    "HF_HUB_DISABLE_PROGRESS_BARS": "1",
    "HF_HUB_VERBOSITY": "error",
    "TRANSFORMERS_VERBOSITY": "error",
    "TOKENIZERS_PARALLELISM": "false",
}


class EmbeddingService:
    def __init__(self):
        self._model: Optional["_ST"] = None

    def _load(self):
        if not self._model:
            # Lazy import — only triggered when RAG/Qdrant actually calls embed().
            # Keep Hub/Transformers noise out of production logs; missing
            # HF_TOKEN is acceptable because this public model can be fetched
            # anonymously, but the warning is not useful on every first query.
            for key, value in _HF_QUIET_DEFAULTS.items():
                os.environ.setdefault(key, value)
            logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
            logging.getLogger("transformers").setLevel(logging.ERROR)

            from sentence_transformers import SentenceTransformer
            logger.info(f"Loading embedding model: {settings.embedding_model}")
            token = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN")
            if token:
                try:
                    self._model = SentenceTransformer(settings.embedding_model, token=token)
                    return
                except TypeError:
                    # Older sentence-transformers releases do not accept token.
                    logger.debug("SentenceTransformer does not support token=; loading without it")
            self._model = SentenceTransformer(settings.embedding_model)

    def embed(self, text: str) -> list[float]:
        self._load()
        return self._model.encode(text, normalize_embeddings=True).tolist()

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self._load()
        return self._model.encode(texts, normalize_embeddings=True, batch_size=32).tolist()


embeddings = EmbeddingService()
