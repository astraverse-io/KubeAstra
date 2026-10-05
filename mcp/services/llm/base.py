"""LLM provider interface.

Providers return plain text from ``generate`` / ``generate_stream`` (backwards
compatible) and ``(text, TokenUsage)`` from ``generate_with_usage`` /
``generate_stream_with_usage`` (Phase 1A). The caller handles prompt
construction, JSON parsing, and fallbacks so providers stay thin and swappable.
"""

import contextvars
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator, Optional, Tuple

from .pricing import TokenUsage, compute_cost


class LLMProviderError(RuntimeError):
    """Raised when a provider cannot fulfill a generation request."""


# ── Native tool-calling (Harness v2) ──────────────────────────────────────────
# A structured alternative to the text-ReAct loop: the provider returns a
# first-class ``{name, arguments}`` object instead of action JSON embedded in
# prose, so the regex "salvage" layers in react.py become unnecessary. Opt-in
# per provider via ``supports_native_tools`` — providers that return False keep
# using the text harness with zero behavior change.


@dataclass
class ToolCall:
    """One structured tool invocation requested by the model.

    ``id`` is the provider's opaque call id, echoed back on the matching tool
    result so multi-call turns stay correlated. ``arguments`` is the decoded
    argument object (already a dict — no prose, no regex salvage).
    """

    id: str
    name: str
    arguments: dict


@dataclass
class ToolTurn:
    """One assistant turn from a native tool-calling provider.

    ``text`` is any prose emitted alongside the calls (often a short rationale).
    ``tool_calls`` is empty when the model chose to answer directly — the
    harness treats that as the final answer. ``stop`` is the provider stop
    reason normalized to one of ``{"tool_calls", "end", "length", "refusal"}``
    (unrecognized reasons pass through verbatim).
    """

    text: str
    tool_calls: list[ToolCall]
    usage: TokenUsage
    stop: str = "end"


_generation_deadline: contextvars.ContextVar[float | None] = (
    contextvars.ContextVar("llm_generation_deadline", default=None)
)


def set_generation_deadline(deadline_monotonic: float) -> contextvars.Token:
    """Install the absolute worker deadline for provider calls in this context."""
    if deadline_monotonic <= 0:
        raise ValueError("deadline_monotonic must be positive")
    return _generation_deadline.set(deadline_monotonic)


def reset_generation_deadline(token: contextvars.Token) -> None:
    _generation_deadline.reset(token)


def effective_timeout(configured_timeout: float) -> float:
    """Return the smaller of provider configuration and worker time remaining."""
    timeout = float(configured_timeout)
    if timeout <= 0:
        raise ValueError("configured_timeout must be positive")
    deadline = _generation_deadline.get()
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LLMProviderError("agent execution deadline expired before LLM call")
    return min(timeout, remaining)


class LLMProvider(ABC):
    """Abstract base class for pluggable LLM backends."""

    name: str = "base"
    model: str = ""  # Subclasses set this to the configured model identifier.

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """Whether the provider is configured and can serve requests."""

    @abstractmethod
    def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate a single completion and return its text."""

    def generate_stream(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> Iterator[str]:
        """Yield text chunks as the model generates them.

        Default implementation falls back to a single-shot generate() and
        yields the whole result at once. Providers should override this
        with a real streaming call when the underlying SDK supports it
        (Gemini's generate_content_stream, Ollama's stream:true, etc.).
        """
        yield self.generate(prompt, system=system, temperature=temperature,
                            max_tokens=max_tokens)

    def generate_with_usage(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> Tuple[str, TokenUsage]:
        """Return ``(text, TokenUsage)`` for cost-tracked single-shot generation.

        Default implementation calls ``generate`` and reports empty usage so
        providers without an instrumented path (Ollama, test fakes) still
        satisfy the interface. Providers that can read usage metadata from
        their SDK response should override this.
        """
        text = self.generate(prompt, system=system, temperature=temperature, max_tokens=max_tokens)
        return text, TokenUsage.empty(model=self.model)

    def generate_stream_with_usage(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> Tuple[Iterator[str], "list[TokenUsage]"]:
        """Return ``(text_iterator, usage_holder)`` for cost-tracked streaming.

        The usage_holder is a list that the iterator appends the final
        ``TokenUsage`` into once streaming completes. Callers should read
        ``usage_holder[0]`` after exhausting the iterator. Default
        implementation wraps ``generate_stream`` with an empty usage report.
        """
        usage_holder: list[TokenUsage] = []

        def _wrap() -> Iterator[str]:
            for chunk in self.generate_stream(
                prompt, system=system, temperature=temperature, max_tokens=max_tokens
            ):
                yield chunk
            usage_holder.append(TokenUsage.empty(model=self.model))

        return _wrap(), usage_holder

    # ── Native tool-calling (Harness v2) ──────────────────────────────────────

    def supports_native_tools(self) -> bool:
        """Whether this provider can return structured tool calls.

        Defaults to ``False`` so providers without a native path (Ollama, test
        fakes) are transparently routed to the text-ReAct harness — no
        regression for local users.
        """
        return False

    def generate_with_tools(
        self,
        messages: "list[dict]",
        tools: "list[dict]",
        system: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> "ToolTurn":
        """Run one native tool-calling turn and return a :class:`ToolTurn`.

        ``messages`` is a provider-neutral conversation; each item is a dict:

        - ``{"role": "user", "content": <str | list>}``
        - ``{"role": "assistant", "content": <str>, "tool_calls": [ToolCall, …]}``
        - ``{"role": "tool", "tool_call_id": <str>, "content": <str>}``

        ``tools`` are provider-neutral specs from
        :func:`tool_registry.build_native_tool_specs` — one
        ``{"name", "description", "input_schema"}`` per tool, where
        ``input_schema`` is the tool's Pydantic JSON schema. Each provider
        adapter maps these to its SDK shape (Claude ``tool_use`` / OpenAI
        ``tools`` / Gemini ``function_declarations``).

        Only providers returning ``True`` from :meth:`supports_native_tools`
        implement this; the default raises so a misroute fails loudly rather
        than silently degrading.
        """
        raise NotImplementedError(
            f"{self.name} provider does not support native tool-calling"
        )
