"""Google Gemini LLM provider."""

import logging
from typing import Any, Iterator, Optional, Tuple

from .base import (
    LLMProvider,
    LLMProviderError,
    ToolCall,
    ToolTurn,
    effective_timeout,
)
from .pricing import TokenUsage, compute_cost

logger = logging.getLogger(__name__)

# Gemini finish_reason → normalized ToolTurn.stop (see ToolTurn docstring).
# Gemini returns STOP even when it emitted function calls, so the parser sets
# "tool_calls" itself when calls are present and only consults this otherwise.
_FINISH_MAP = {
    "STOP": "end",
    "MAX_TOKENS": "length",
    "SAFETY": "refusal",
    "RECITATION": "refusal",
    "PROHIBITED_CONTENT": "refusal",
    "BLOCKLIST": "refusal",
}


def _gemini_function_declaration(types: Any, spec: dict) -> Any:
    """Build a ``types.FunctionDeclaration`` from a provider-neutral tool spec.

    Prefers ``parameters_json_schema`` (raw JSON-schema passthrough, so the
    Pydantic schema goes through untouched); falls back to ``parameters`` on
    older SDKs that lack that field.
    """
    fields = getattr(types.FunctionDeclaration, "model_fields", {})
    kwargs: dict = {"name": spec["name"], "description": spec.get("description", "")}
    if "parameters_json_schema" in fields:
        kwargs["parameters_json_schema"] = spec["input_schema"]
    else:  # pragma: no cover - exercised only on google-genai < ~1.5
        kwargs["parameters"] = spec["input_schema"]
    return types.FunctionDeclaration(**kwargs)


def _to_gemini_contents(types: Any, messages: list[dict]) -> list[Any]:
    """Map provider-neutral messages to Gemini ``contents``.

    - ``user`` → ``Content(role="user", parts=[text])``.
    - ``assistant`` → ``Content(role="model", …)`` with any prose plus one
      ``function_call`` part per :class:`ToolCall`.
    - ``tool`` → a ``function_response`` part keyed by the function **name**
      (Gemini has no call-id correlation); non-dict content is wrapped as
      ``{"result": <content>}`` since the API requires a struct.
    """
    contents: list[Any] = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            contents.append(types.Content(role="user", parts=[types.Part(text=m.get("content", ""))]))
        elif role == "assistant":
            parts: list[Any] = []
            if m.get("content"):
                parts.append(types.Part(text=m["content"]))
            for tc in m.get("tool_calls") or []:
                name = tc.name if isinstance(tc, ToolCall) else tc["name"]
                args = tc.arguments if isinstance(tc, ToolCall) else tc.get("arguments", {})
                parts.append(types.Part(function_call=types.FunctionCall(name=name, args=args or {})))
            contents.append(types.Content(role="model", parts=parts))
        elif role == "tool":
            raw = m.get("content", "")
            response = raw if isinstance(raw, dict) else {"result": raw}
            contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                name=m.get("name", "") or "", response=response
                            )
                        )
                    ],
                )
            )
        else:
            raise LLMProviderError(f"Unknown message role for Gemini: {role!r}")
    return contents


def _finish_name(finish: Any) -> Optional[str]:
    """Normalize a Gemini finish_reason (enum or str) to its string name."""
    if finish is None:
        return None
    return getattr(finish, "name", None) or (finish if isinstance(finish, str) else str(finish))


def _parse_gemini_tool_turn(response: Any, model: str) -> ToolTurn:
    """Parse a Gemini response into a :class:`ToolTurn`.

    Walks ``candidates[0].content.parts`` collecting ``function_call`` parts into
    :class:`ToolCall` objects and ``text`` parts into prose. A safety/recitation
    finish with no usable output is surfaced as an error.
    """
    candidates = getattr(response, "candidates", None) or []
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    finish = None
    if candidates:
        cand = candidates[0]
        finish = getattr(cand, "finish_reason", None)
        content = getattr(cand, "content", None)
        for part in getattr(content, "parts", None) or []:
            fc = getattr(part, "function_call", None)
            if fc is not None:
                tool_calls.append(
                    ToolCall(
                        id=getattr(fc, "id", "") or "",
                        name=getattr(fc, "name", "") or "",
                        arguments=dict(getattr(fc, "args", None) or {}),
                    )
                )
                continue
            text = getattr(part, "text", None)
            if text:
                text_parts.append(text)

    stop = "tool_calls" if tool_calls else _FINISH_MAP.get(_finish_name(finish), "end")
    if stop == "refusal" and not tool_calls and not text_parts:
        raise LLMProviderError(f"Gemini declined the request (finish_reason={_finish_name(finish)})")

    return ToolTurn(
        text="".join(text_parts).strip(),
        tool_calls=tool_calls,
        usage=_extract_usage(response, model),
        stop=stop,
    )


def _extract_usage(response_or_chunk: Any, model: str) -> TokenUsage:
    """Pull token counts off a Gemini SDK response / final stream chunk.

    Gemini reports usage at ``response.usage_metadata`` with these fields:
    ``prompt_token_count`` (total input), ``cached_content_token_count``
    (the cached subset of prompt_token_count), ``candidates_token_count``
    (output). Missing fields default to 0 so partial reports degrade
    gracefully into best-effort attribution.
    """
    meta = getattr(response_or_chunk, "usage_metadata", None)
    if meta is None:
        return TokenUsage.empty(model=model)
    tokens_in = int(getattr(meta, "prompt_token_count", 0) or 0)
    cached = int(getattr(meta, "cached_content_token_count", 0) or 0)
    tokens_out = int(getattr(meta, "candidates_token_count", 0) or 0)
    usage = TokenUsage(
        tokens_in=tokens_in,
        cached_tokens_in=cached,
        tokens_out=tokens_out,
        model=model,
    )
    usage.cost_usd = compute_cost(usage)
    return usage


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str, timeout: int = 60):
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self.model = model  # Expose for TokenUsage tagging.
        self._client: Optional[Any] = None

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def _get_client(self, timeout_seconds: float | None = None) -> Optional[Any]:
        effective = (
            self._timeout
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        use_cached_client = effective >= self._timeout
        if use_cached_client and self._client is not None:
            return self._client
        if not self._api_key:
            return None
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            logger.error("google-genai is not installed")
            raise LLMProviderError("google-genai SDK is not installed") from exc
        client = genai.Client(
            api_key=self._api_key,
            http_options=types.HttpOptions(timeout=effective * 1000),
        )
        if use_cached_client:
            self._client = client
        return client

    def _raw_generate(
        self,
        prompt: str,
        system: Optional[str],
        temperature: float,
        max_tokens: Optional[int],
    ) -> Any:
        """Single-shot call returning the raw SDK response (so callers can
        either read ``.text`` for back-compat or ``.usage_metadata`` for
        cost tracking)."""
        client = self._get_client(effective_timeout(self._timeout))
        if client is None:
            raise LLMProviderError("Gemini API key is not configured")

        try:
            from google.genai import types
        except ImportError as exc:
            raise LLMProviderError("google-genai SDK is not installed") from exc

        kwargs: dict = {"temperature": temperature}
        if system:
            kwargs["system_instruction"] = system
        if max_tokens:
            kwargs["max_output_tokens"] = max_tokens

        try:
            return client.models.generate_content(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(**kwargs),
            )
        except Exception as exc:
            logger.error("Gemini request failed: %s", exc)
            raise LLMProviderError(str(exc)) from exc

    def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> str:
        response = self._raw_generate(prompt, system, temperature, max_tokens)
        return response.text or ""

    def generate_with_usage(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> Tuple[str, TokenUsage]:
        response = self._raw_generate(prompt, system, temperature, max_tokens)
        return (response.text or ""), _extract_usage(response, self._model)

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
        client = self._get_client(effective_timeout(self._timeout))
        if client is None:
            raise LLMProviderError("Gemini API key is not configured")

        try:
            from google.genai import types
        except ImportError as exc:
            raise LLMProviderError("google-genai SDK is not installed") from exc

        config_kwargs: dict = {}
        if system:
            config_kwargs["system_instruction"] = system
        if max_tokens:
            config_kwargs["max_output_tokens"] = max_tokens
        if tools:
            config_kwargs["tools"] = [
                types.Tool(
                    function_declarations=[_gemini_function_declaration(types, t) for t in tools]
                )
            ]

        try:
            response = client.models.generate_content(
                model=self._model,
                contents=_to_gemini_contents(types, messages),
                config=types.GenerateContentConfig(**config_kwargs),
            )
        except LLMProviderError:
            raise
        except Exception as exc:
            logger.error("Gemini tool request failed: %s", exc)
            raise LLMProviderError(str(exc)) from exc

        return _parse_gemini_tool_turn(response, self._model)

    def generate_stream(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> Iterator[str]:
        """Stream Gemini output chunks as they are generated."""
        for text, _ in self._stream_with_chunks(prompt, system, temperature, max_tokens):
            if text:
                yield text

    def generate_stream_with_usage(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> Tuple[Iterator[str], "list[TokenUsage]"]:
        """Stream chunks AND capture final usage metadata.

        Gemini emits ``usage_metadata`` on the final chunk of the stream. The
        returned ``usage_holder`` is appended to once streaming completes;
        callers read ``usage_holder[0]`` after exhausting the iterator.
        """
        usage_holder: list[TokenUsage] = []

        def _wrap() -> Iterator[str]:
            last_chunk: Any = None
            try:
                for text, chunk in self._stream_with_chunks(prompt, system, temperature, max_tokens):
                    last_chunk = chunk
                    yield text
            finally:
                usage_holder.append(_extract_usage(last_chunk, self._model))

        return _wrap(), usage_holder

    def _stream_with_chunks(
        self,
        prompt: str,
        system: Optional[str],
        temperature: float,
        max_tokens: Optional[int],
    ) -> Iterator[Tuple[str, Any]]:
        """Yield ``(text, raw_chunk)`` pairs so usage-aware callers can grab
        the final chunk's metadata without re-iterating."""
        client = self._get_client(effective_timeout(self._timeout))
        if client is None:
            raise LLMProviderError("Gemini API key is not configured")

        try:
            from google.genai import types
        except ImportError as exc:
            raise LLMProviderError("google-genai SDK is not installed") from exc

        kwargs: dict = {"temperature": temperature}
        if system:
            kwargs["system_instruction"] = system
        if max_tokens:
            kwargs["max_output_tokens"] = max_tokens

        try:
            stream = client.models.generate_content_stream(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(**kwargs),
            )
            for chunk in stream:
                # Streaming SDK timeouts may behave as per-read timeouts.
                # Re-check the request-local absolute deadline on every chunk
                # so a healthy but endless stream cannot hold the worker.
                effective_timeout(self._timeout)
                text = getattr(chunk, "text", None) or ""
                # Yield even empty chunks so the caller sees the last one
                # (the one carrying usage_metadata).
                yield text, chunk
        except Exception as exc:
            logger.error("Gemini stream failed: %s", exc)
            raise LLMProviderError(str(exc)) from exc
