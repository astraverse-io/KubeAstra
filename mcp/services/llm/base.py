"""LLM provider interface.

Providers return plain text from ``generate`` / ``generate_stream`` (backwards
compatible) and ``(text, TokenUsage)`` from ``generate_with_usage`` /
``generate_stream_with_usage`` (Phase 1A). The caller handles prompt
construction, JSON parsing, and fallbacks so providers stay thin and swappable.
"""

import contextvars
import time
from abc import ABC, abstractmethod
from typing import Iterator, Optional, Tuple

from .pricing import TokenUsage, compute_cost


class LLMProviderError(RuntimeError):
    """Raised when a provider cannot fulfill a generation request."""


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
