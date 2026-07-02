"""OpenAI provider.

Uses the Chat Completions HTTP API via httpx (same dependency footprint as
the Ollama provider). Requires OPENAI_API_KEY. OPENAI_BASE_URL can point at
any OpenAI-compatible endpoint (Azure OpenAI gateways, vLLM, LiteLLM, ...).
"""

import logging
from typing import Any, Optional, Tuple

import httpx

from .base import LLMProvider, LLMProviderError, effective_timeout
from .pricing import TokenUsage, compute_cost

logger = logging.getLogger(__name__)


def _extract_usage(payload: dict, model: str) -> TokenUsage:
    """Pull token counts off an OpenAI Chat Completions response body.

    OpenAI reports usage at ``.usage`` with ``prompt_tokens`` /
    ``completion_tokens``. Some deployments expose
    ``prompt_tokens_details.cached_tokens`` for prompt-cache hits; when
    present it's treated as the cached subset of prompt_tokens.
    """
    usage = payload.get("usage") or {}
    tokens_in = int(usage.get("prompt_tokens") or 0)
    tokens_out = int(usage.get("completion_tokens") or 0)
    details = usage.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    result = TokenUsage(
        tokens_in=tokens_in,
        cached_tokens_in=cached,
        tokens_out=tokens_out,
        model=model,
    )
    result.cost_usd = compute_cost(result)
    return result


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 120.0,
    ):
        self._api_key = api_key or ""
        self._model = model
        self._base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._timeout = timeout
        self.model = model  # Expose for TokenUsage tagging.

    @property
    def enabled(self) -> bool:
        return bool(self._api_key and self._model)

    def _raw_generate(
        self,
        prompt: str,
        system: Optional[str],
        temperature: float,
        max_tokens: Optional[int],
    ) -> dict:
        if not self.enabled:
            raise LLMProviderError("OPENAI_API_KEY is not configured")

        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_completion_tokens"] = max_tokens

        url = f"{self._base_url}/chat/completions"
        try:
            response = httpx.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=effective_timeout(self._timeout),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("OpenAI request to %s failed: %s", url, exc)
            raise LLMProviderError(f"OpenAI request failed: {exc}") from exc

        return response.json()

    @staticmethod
    def _response_text(data: dict) -> str:
        choices = data.get("choices") or []
        content = ""
        if choices:
            content = (choices[0].get("message") or {}).get("content", "") or ""
        if not content:
            raise LLMProviderError("OpenAI returned an empty response")
        return content

    def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> str:
        data = self._raw_generate(prompt, system, temperature, max_tokens)
        return self._response_text(data)

    def generate_with_usage(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> Tuple[str, TokenUsage]:
        data = self._raw_generate(prompt, system, temperature, max_tokens)
        return self._response_text(data), _extract_usage(data, self._model)
