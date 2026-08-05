"""Embeddings for semantic similarity search, with a pluggable backend.

Three backends, chosen by ``EMBEDDINGS_MODE``:

  local   sentence-transformers in-process. Accurate and offline, but pulls in
          torch and friends (~770MB installed). Server deployments only — the
          desktop bundle excludes torch entirely.
  api     the provider's embeddings HTTP endpoint. The desktop default: the
          user already supplied an API key, and embedding a runbook costs
          fractions of a cent.
  ollama  a local Ollama daemon, for airgapped desktops.

``embeddings`` — the module-level singleton at the bottom — is the only public
surface. Eight call sites do ``from services.embeddings import embeddings``, so
backends swap *behind* it; do not replace it with a factory.

Anthropic publishes no embeddings API. When the chat provider is Anthropic the
user supplies a separate embeddings key; if they decline, ``embed*`` raises
:class:`EmbeddingsUnavailable` and retrieval degrades to keyword-only rather
than breaking the investigation.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Optional, Protocol

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

# Native vector width per provider/model. These MUST match what the provider
# returns: the value is written into the Qdrant collection and a mismatch
# corrupts recall silently rather than erroring.
PROVIDER_DEFAULTS: dict[str, tuple[str, int]] = {
    "voyage": ("voyage-3", 1024),
    "openai": ("text-embedding-3-small", 1536),
    "gemini": ("text-embedding-004", 768),
    "ollama": ("nomic-embed-text", 768),
}

_RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3


class EmbeddingsUnavailable(RuntimeError):
    """No embeddings backend is usable.

    Callers in the retrieval path must catch this and fall back to keyword
    search. It is never fatal to an investigation.
    """


class _Backend(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]: ...

    def embed_many(self, texts: list[str]) -> list[list[float]]: ...


class _LocalBackend:
    """sentence-transformers, imported lazily.

    The lazy import is load-bearing beyond size: torch calls getpwuid() at
    import time, which raises when the container UID is absent from
    /etc/passwd. Deferring it keeps startup working where RAG is unused.
    """

    def __init__(self) -> None:
        self._model: Optional["_ST"] = None
        self.dim = settings.embedding_dim

    def _load(self):
        if self._model:
            return
        for key, value in _HF_QUIET_DEFAULTS.items():
            os.environ.setdefault(key, value)
        logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
        logging.getLogger("transformers").setLevel(logging.ERROR)

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            # Expected in desktop bundles, which exclude torch on purpose.
            raise EmbeddingsUnavailable(
                "sentence-transformers is not installed; set EMBEDDINGS_MODE=api "
                "or EMBEDDINGS_MODE=ollama"
            ) from exc

        logger.info("Loading embedding model: %s", settings.embedding_model)
        token = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN")
        if token:
            try:
                self._model = SentenceTransformer(settings.embedding_model, token=token)
                return
            except TypeError:
                logger.debug("SentenceTransformer has no token= kwarg; loading without")
        self._model = SentenceTransformer(settings.embedding_model)

    def embed(self, text: str) -> list[float]:
        self._load()
        return self._model.encode(text, normalize_embeddings=True).tolist()

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self._load()
        return self._model.encode(
            texts, normalize_embeddings=True, batch_size=32
        ).tolist()


class _HttpBackend:
    """Shared retry/transport behaviour for the HTTP-backed backends.

    Subclasses implement `_request`, returning vectors in input order.
    """

    provider = ""

    def __init__(self, model: str, dim: int, timeout: float) -> None:
        self.model = model
        self.dim = dim
        self.timeout = timeout

    def _request(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
        raise NotImplementedError

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        last: Optional[Exception] = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                return self._request(texts)
            except _RetryableHttpError as exc:
                last = exc
                if attempt == _MAX_ATTEMPTS - 1:
                    break
                delay = 2**attempt
                logger.warning(
                    "%s embeddings %s; retrying in %ss (attempt %d/%d)",
                    self.provider, exc, delay, attempt + 1, _MAX_ATTEMPTS,
                )
                time.sleep(delay)
            except Exception as exc:
                raise EmbeddingsUnavailable(
                    f"{self.provider} embeddings failed: {exc}"
                ) from exc
        raise EmbeddingsUnavailable(
            f"{self.provider} embeddings unavailable after "
            f"{_MAX_ATTEMPTS} attempts: {last}"
        )


class _RetryableHttpError(RuntimeError):
    """Transient upstream failure worth retrying (429 / 5xx / timeout)."""


class _NullBackend:
    """Placeholder when no embeddings source is configured.

    Exists so the app starts and investigates normally with memory in
    keyword-only mode, instead of failing at import or on first search.
    """

    dim = 0

    def _fail(self):
        raise EmbeddingsUnavailable(
            "No embeddings provider is configured. Investigation memory is "
            "running in keyword-only mode; add an embeddings key in Settings "
            "to enable semantic recall."
        )

    def embed(self, text: str) -> list[float]:
        self._fail()

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self._fail()


class EmbeddingService:
    """Dispatcher. Resolves a backend on first use and caches it."""

    def __init__(self) -> None:
        self._backend: Optional[_Backend] = None

    # ── backend selection ────────────────────────────────────────────────

    def _resolve(self) -> _Backend:
        if self._backend is None:
            self._backend = self._build()
        return self._backend

    def _build(self) -> _Backend:
        mode = (settings.embeddings_mode or "local").strip().lower()

        if mode == "ollama":
            model, dim = self._model_for("ollama")
            return _OllamaBackend(model, dim, settings.embeddings_timeout_seconds)

        if mode == "api":
            provider = (settings.embeddings_provider or "").strip().lower()
            if provider not in ("voyage", "openai", "gemini"):
                logger.warning(
                    "EMBEDDINGS_MODE=api but EMBEDDINGS_PROVIDER=%r is not a "
                    "provider with an embeddings API; memory is keyword-only",
                    provider,
                )
                return _NullBackend()
            api_key = self._api_key(provider)
            if not api_key:
                logger.info(
                    "No embeddings key stored for %s; memory is keyword-only",
                    provider,
                )
                return _NullBackend()
            model, dim = self._model_for(provider)
            backend_cls = {
                "voyage": _VoyageBackend,
                "openai": _OpenAIBackend,
                "gemini": _GeminiBackend,
            }[provider]
            return backend_cls(
                model, dim, settings.embeddings_timeout_seconds, api_key
            )

        return _LocalBackend()

    @staticmethod
    def _model_for(provider: str) -> tuple[str, int]:
        default_model, default_dim = PROVIDER_DEFAULTS[provider]
        model = (settings.embeddings_model or "").strip() or default_model
        # A non-default model may have a different width; the operator must
        # set EMBEDDING_DIM to match, and the collection guard checks it.
        dim = default_dim if model == default_model else settings.embedding_dim
        return model, dim

    @staticmethod
    def _api_key(provider: str) -> str:
        """Env var first (server deployments), then the desktop keychain."""
        env_names = {
            "voyage": ("VOYAGE_API_KEY",),
            "openai": ("EMBEDDINGS_API_KEY", "OPENAI_API_KEY"),
            "gemini": ("EMBEDDINGS_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
        }[provider]
        for name in env_names:
            value = os.environ.get(name)
            if value:
                return value
        try:
            import desktop_secrets  # only importable in the backend process
        except ImportError:
            return ""
        return desktop_secrets.get_secret(f"embeddings.{provider}") or ""

    # ── public surface ───────────────────────────────────────────────────

    def reset(self) -> None:
        """Drop the cached backend. Call after keys or mode change, or the
        process keeps using the previous credentials until restart."""
        self._backend = None

    @property
    def dim(self) -> int:
        return self._resolve().dim

    @property
    def available(self) -> bool:
        """True when semantic recall is usable.

        Lets the UI report memory status without triggering a failing embed
        call — so it must not claim availability the first embed would refuse.
        Constructing _LocalBackend does not import sentence-transformers (that
        is deliberately deferred), so check the package is importable here;
        find_spec does not execute the module, so torch is not loaded.
        """
        backend = self._resolve()
        if isinstance(backend, _NullBackend):
            return False
        if isinstance(backend, _LocalBackend):
            from importlib.util import find_spec

            return find_spec("sentence_transformers") is not None
        probe = getattr(backend, "ready", None)
        if probe is not None:
            return probe()
        return True

    def embed(self, text: str) -> list[float]:
        return self._resolve().embed(text)

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return self._resolve().embed_many(texts)


# ── HTTP backends ─────────────────────────────────────────────────────────
# Request/response shapes follow each provider's published embeddings API.
# Verify against current provider docs when touching these; the surrounding
# retry/degradation behaviour is ours and is covered by tests.


def _get_json(url: str, timeout: float) -> dict:
    """Plain GET, for readiness probes rather than the embed path.

    Deliberately not sharing `_post_json`'s retry classification: a probe that
    retries is a probe that hangs the settings screen.
    """
    import httpx

    response = httpx.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    import httpx

    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise _RetryableHttpError(f"timeout after {timeout}s") from exc
    except httpx.HTTPError as exc:
        raise _RetryableHttpError(str(exc)) from exc

    if response.status_code in _RETRY_STATUSES:
        raise _RetryableHttpError(f"HTTP {response.status_code}")
    if response.status_code >= 400:
        # 401/403/404 are configuration errors — retrying cannot fix them.
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
    return response.json()


class _VoyageBackend(_HttpBackend):
    provider = "voyage"

    def __init__(self, model: str, dim: int, timeout: float, api_key: str) -> None:
        super().__init__(model, dim, timeout)
        self.api_key = api_key

    def _request(self, texts: list[str]) -> list[list[float]]:
        data = _post_json(
            "https://api.voyageai.com/v1/embeddings",
            {"input": texts, "model": self.model},
            {"Authorization": f"Bearer {self.api_key}"},
            self.timeout,
        )
        return [item["embedding"] for item in data["data"]]


class _OpenAIBackend(_HttpBackend):
    provider = "openai"

    def __init__(self, model: str, dim: int, timeout: float, api_key: str) -> None:
        super().__init__(model, dim, timeout)
        self.api_key = api_key

    def _request(self, texts: list[str]) -> list[list[float]]:
        data = _post_json(
            "https://api.openai.com/v1/embeddings",
            {"input": texts, "model": self.model},
            {"Authorization": f"Bearer {self.api_key}"},
            self.timeout,
        )
        # OpenAI documents `data` as sorted by index, but sort defensively:
        # a reordered response would silently mis-associate vectors.
        ordered = sorted(data["data"], key=lambda item: item["index"])
        return [item["embedding"] for item in ordered]


class _GeminiBackend(_HttpBackend):
    provider = "gemini"

    def __init__(self, model: str, dim: int, timeout: float, api_key: str) -> None:
        super().__init__(model, dim, timeout)
        self.api_key = api_key

    def _request(self, texts: list[str]) -> list[list[float]]:
        model_path = f"models/{self.model}"
        data = _post_json(
            f"https://generativelanguage.googleapis.com/v1beta/"
            f"{model_path}:batchEmbedContents",
            {
                "requests": [
                    {"model": model_path, "content": {"parts": [{"text": text}]}}
                    for text in texts
                ]
            },
            {"x-goog-api-key": self.api_key},
            self.timeout,
        )
        return [item["values"] for item in data["embeddings"]]


class _OllamaBackend(_HttpBackend):
    provider = "ollama"

    def _request(self, texts: list[str]) -> list[list[float]]:
        base = settings.ollama_url.rstrip("/")
        data = _post_json(
            f"{base}/api/embed",
            {"model": self.model, "input": texts},
            {},
            self.timeout,
        )
        return data["embeddings"]

    def ready(self) -> bool:
        """Whether an embed call would work, without making one.

        Two things can be missing independently: the daemon, and the model.
        `ollama serve` running says nothing about whether anyone has run
        `ollama pull nomic-embed-text` — and the embedding model is not the
        chat model, so a user with a working Ollama setup very likely does not
        have it.

        Consulted by `EmbeddingService.available`, which must not report
        vector memory the first embed would refuse.
        """
        base = settings.ollama_url.rstrip("/")
        try:
            data = _get_json(f"{base}/api/tags", self.timeout)
        except Exception:
            logger.debug("Ollama tag listing failed", exc_info=True)
            return False

        # Ollama reports `nomic-embed-text:latest` for a bare model name.
        wanted = self.model.split(":")[0]
        return any(
            str(entry.get("name", "")).split(":")[0] == wanted
            for entry in data.get("models", [])
        )


embeddings = EmbeddingService()
