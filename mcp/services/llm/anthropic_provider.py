"""Anthropic Claude provider.

Uses the official ``anthropic`` SDK (Messages API). Requires ANTHROPIC_API_KEY.

Notes on parameter mapping:
- ``temperature`` is intentionally NOT forwarded — sampling parameters are
  rejected (400) on Claude Opus 4.7+ models; prompting controls behavior.
- The ``thinking`` parameter is omitted so the caller's small ``max_tokens``
  budgets (the ReAct loop sends 800-2500) are spent entirely on the visible
  answer rather than on reasoning tokens.
"""

import logging
from typing import Any, Optional, Tuple

from .base import LLMProvider, LLMProviderError, effective_timeout
from .pricing import TokenUsage, compute_cost

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 4096


def _extract_usage(response: Any, model: str) -> TokenUsage:
    """Pull token counts off an Anthropic SDK Messages response.

    Anthropic reports usage at ``response.usage`` with ``input_tokens`` /
    ``output_tokens`` / ``cache_read_input_tokens`` (subset of input served
    from prompt cache). Missing fields default to 0 so partial reports
    degrade gracefully.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage.empty(model=model)
    tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
    cached = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
    result = TokenUsage(
        tokens_in=tokens_in,
        cached_tokens_in=cached,
        tokens_out=tokens_out,
        model=model,
    )
    result.cost_usd = compute_cost(result)
    return result


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: float = 120.0):
        self._api_key = api_key or ""
        self._model = model
        self._timeout = timeout
        self.model = model  # Expose for TokenUsage tagging.
        self._client: Optional[Any] = None

    @property
    def enabled(self) -> bool:
        return bool(self._api_key and self._model)

    def _get_client(self, timeout_seconds: float | None = None):
        effective = self._timeout if timeout_seconds is None else float(timeout_seconds)
        use_cached_client = effective >= self._timeout
        if use_cached_client and self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:
            raise LLMProviderError(
                "The 'anthropic' package is not installed. Run: pip install anthropic"
            ) from exc
        client = anthropic.Anthropic(api_key=self._api_key, timeout=effective)
        if use_cached_client:
            self._client = client
        return client

    def _raw_generate(
        self,
        prompt: str,
        system: Optional[str],
        max_tokens: Optional[int],
    ) -> Any:
        if not self.enabled:
            raise LLMProviderError("ANTHROPIC_API_KEY is not configured")

        try:
            import anthropic
        except ImportError as exc:
            raise LLMProviderError(
                "The 'anthropic' package is not installed. Run: pip install anthropic"
            ) from exc

        client = self._get_client(effective_timeout(self._timeout))
        kwargs: dict = {
            "model": self._model,
            "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system

        try:
            return client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            logger.error("Anthropic request failed: %s", exc)
            raise LLMProviderError(f"Anthropic request failed: {exc}") from exc

    @staticmethod
    def _response_text(response: Any) -> str:
        # Safety classifiers can decline a request with HTTP 200 — check
        # stop_reason before reading content.
        if getattr(response, "stop_reason", None) == "refusal":
            raise LLMProviderError("Anthropic declined the request (stop_reason=refusal)")

        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()
        if not text:
            raise LLMProviderError("Anthropic returned an empty response")
        return text

    def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> str:
        response = self._raw_generate(prompt, system, max_tokens)
        return self._response_text(response)

    def generate_with_usage(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> Tuple[str, TokenUsage]:
        response = self._raw_generate(prompt, system, max_tokens)
        return self._response_text(response), _extract_usage(response, self._model)
