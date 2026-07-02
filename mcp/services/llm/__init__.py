"""Pluggable LLM provider abstraction."""

from .base import LLMProvider

HARDCODED_GEMINI_MODEL = "gemini-3.1-flash-lite"


def get_provider(model: str | None = None) -> LLMProvider:
    """Return the configured LLM provider instance."""
    from config.settings import get_settings

    settings = get_settings()
    name = (settings.llm_provider or "gemini").lower()
    selected_model = (model or "").strip()

    if name == "ollama":
        from .ollama_provider import OllamaProvider

        return OllamaProvider(
            base_url=settings.ollama_base_url,
            model=selected_model or settings.ollama_model,
            auth_token=settings.ollama_auth_token,
            timeout=settings.ollama_timeout_seconds,
            num_ctx=settings.ollama_num_ctx,
        )

    if name == "gemini":
        from .gemini_provider import GeminiProvider

        return GeminiProvider(
            api_key=settings.gemini_api_key,
            model=HARDCODED_GEMINI_MODEL,
            timeout=settings.gemini_timeout_seconds,
        )

    raise ValueError(
        f"Unknown LLM_PROVIDER '{settings.llm_provider}'. Supported: 'gemini', 'ollama'."
    )


__all__ = ["LLMProvider", "get_provider"]
