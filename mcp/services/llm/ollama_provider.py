"""Ollama local or self-hosted LLM provider."""

import json
import logging
from typing import Iterator, Optional

import httpx

from .base import LLMProvider, LLMProviderError, effective_timeout

logger = logging.getLogger(__name__)


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        auth_token: str = "",
        timeout: float = 120.0,
        num_ctx: Optional[int] = None,
    ):
        self._base_url = (base_url or "").rstrip("/")
        self._model = model
        self.model = model  # Expose for TokenUsage tagging.
        self._auth_token = auth_token
        self._timeout = timeout
        self._num_ctx = num_ctx

    @property
    def enabled(self) -> bool:
        return bool(self._base_url and self._model)

    def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> str:
        if not self.enabled:
            raise LLMProviderError("Ollama base URL or model is not configured")

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        options: dict = {"temperature": temperature}
        if max_tokens:
            options["num_predict"] = max_tokens
        if self._num_ctx is not None:
            options["num_ctx"] = self._num_ctx

        headers = {}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"

        try:
            response = httpx.post(
                f"{self._base_url}/api/chat",
                headers=headers or None,
                json={
                    "model": self._model,
                    "messages": messages,
                    "stream": False,
                    "options": options,
                },
                timeout=effective_timeout(self._timeout),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Ollama request failed: %s", exc)
            raise LLMProviderError(f"Ollama request failed: {exc}") from exc

        message = response.json().get("message") or {}
        content = message.get("content", "")
        if not content:
            raise LLMProviderError(
                "Ollama returned an empty response; verify the model is pulled"
            )
        return content

    def generate_stream(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> Iterator[str]:
        """Stream Ollama output via /api/chat with stream=true.

        Ollama returns newline-delimited JSON objects, each containing a
        `message.content` chunk plus a final `done: true` object.
        """
        if not self.enabled:
            raise LLMProviderError("Ollama base URL or model is not configured")

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        options: dict = {"temperature": temperature}
        if max_tokens:
            options["num_predict"] = max_tokens
        if self._num_ctx is not None:
            options["num_ctx"] = self._num_ctx

        headers = {}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"

        try:
            with httpx.stream(
                "POST",
                f"{self._base_url}/api/chat",
                headers=headers or None,
                json={
                    "model": self._model,
                    "messages": messages,
                    "stream": True,
                    "options": options,
                },
                timeout=effective_timeout(self._timeout),
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    # httpx streaming timeouts are per I/O operation, not a
                    # total wall-clock cap. Enforce the worker deadline on
                    # every received frame as well.
                    effective_timeout(self._timeout)
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # tolerate the occasional partial frame
                    chunk_msg = obj.get("message") or {}
                    chunk_text = chunk_msg.get("content") or ""
                    if chunk_text:
                        yield chunk_text
                    if obj.get("done"):
                        break
        except httpx.HTTPError as exc:
            logger.error("Ollama stream failed: %s", exc)
            raise LLMProviderError(f"Ollama stream failed: {exc}") from exc
