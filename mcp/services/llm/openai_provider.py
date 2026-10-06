"""OpenAI provider.

Uses the Chat Completions HTTP API via httpx (same dependency footprint as
the Ollama provider). Requires OPENAI_API_KEY. OPENAI_BASE_URL can point at
any OpenAI-compatible endpoint (Azure OpenAI gateways, vLLM, LiteLLM, ...).
"""

import json
import logging
from typing import Any, Optional, Tuple

import httpx

from .base import (
    LLMProvider,
    LLMProviderError,
    ToolCall,
    ToolTurn,
    effective_timeout,
)
from .pricing import TokenUsage, compute_cost

logger = logging.getLogger(__name__)

# OpenAI finish_reason → normalized ToolTurn.stop (see ToolTurn docstring).
_STOP_MAP = {
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "stop": "end",
    "length": "length",
    "content_filter": "refusal",
}


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


def _to_openai_messages(messages: list[dict], system: Optional[str]) -> list[dict]:
    """Map provider-neutral messages to OpenAI Chat Completions ``messages``.

    - ``system`` (if given) becomes the leading ``system`` turn.
    - ``user`` → a user turn.
    - ``assistant`` → an assistant turn; each :class:`ToolCall` becomes an
      OpenAI ``tool_calls`` entry (arguments re-encoded as a JSON string).
    - ``tool`` → a ``tool`` turn keyed by ``tool_call_id``.
    """
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": m.get("content", "")})
        elif role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
            calls = m.get("tool_calls") or []
            if calls:
                entry["tool_calls"] = []
                for tc in calls:
                    call_id = tc.id if isinstance(tc, ToolCall) else tc["id"]
                    name = tc.name if isinstance(tc, ToolCall) else tc["name"]
                    args = tc.arguments if isinstance(tc, ToolCall) else tc.get("arguments", {})
                    entry["tool_calls"].append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args or {})},
                        }
                    )
            out.append(entry)
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id", ""),
                    "content": m.get("content", ""),
                }
            )
        else:
            raise LLMProviderError(f"Unknown message role for OpenAI: {role!r}")
    return out


def _parse_openai_tool_turn(data: dict, model: str) -> ToolTurn:
    """Parse an OpenAI Chat Completions body into a :class:`ToolTurn`.

    OpenAI returns tool calls on ``message.tool_calls`` with ``function.arguments``
    as a JSON *string*; malformed argument JSON degrades to ``{}`` with a warning
    rather than crashing the turn.
    """
    choices = data.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    finish = choice.get("finish_reason")

    tool_calls: list[ToolCall] = []
    for raw in message.get("tool_calls") or []:
        fn = raw.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except (ValueError, TypeError) as exc:
            logger.warning("OpenAI tool-call arguments were not valid JSON (%s): %r", exc, raw_args)
            args = {}
        tool_calls.append(ToolCall(id=raw.get("id", "") or "", name=fn.get("name", "") or "", arguments=args))

    return ToolTurn(
        text=(message.get("content") or "").strip(),
        tool_calls=tool_calls,
        usage=_extract_usage(data, model),
        stop=_STOP_MAP.get(finish, finish or "end"),
    )


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

    # ── Native tool-calling (Harness v2) ──────────────────────────────────────

    def supports_native_tools(self) -> bool:
        return True

    def generate_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> ToolTurn:
        if not self.enabled:
            raise LLMProviderError("OPENAI_API_KEY is not configured")

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": _to_openai_messages(messages, system),
        }
        if max_tokens:
            payload["max_completion_tokens"] = max_tokens
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t["input_schema"],
                    },
                }
                for t in tools
            ]

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
            logger.error("OpenAI tool request to %s failed: %s", url, exc)
            raise LLMProviderError(f"OpenAI request failed: {exc}") from exc

        return _parse_openai_tool_turn(response.json(), self._model)
