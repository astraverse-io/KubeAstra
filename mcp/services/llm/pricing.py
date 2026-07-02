"""Token usage + cost pricing for the LLM providers.

A ``TokenUsage`` value carries everything the recorder needs to attribute spend
to a specific run/step. ``compute_cost`` applies the model-specific input/output
rate from ``PRICE_PER_1K_TOKENS``, with cached-input tokens priced at
``CACHE_RATE`` of the fresh rate (Gemini bills cached prompt tokens at ~25%).

Pricing is a hardcoded table on purpose — under version control, requires a
PR to update, adequate while the model menu changes infrequently. If/when an
unknown model is used we log a WARNING and bill it at zero so a missing entry
doesn't silently inflate or zero cost.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Gemini bills cached prompt tokens at ~25% of fresh input pricing. If a
# provider doesn't report cached tokens, ``cached_tokens_in`` stays 0 and the
# full prompt is billed at the fresh rate.
CACHE_RATE = 0.25

# USD per 1,000 tokens. Update via PR when adding a model. Keys must match the
# value returned by ``provider.model`` (or whatever the provider tags usage with).
PRICE_PER_1K_TOKENS: dict[str, dict[str, float]] = {
    # Gemini 2.5 family (kept for cost-history compatibility)
    "gemini-2.5-flash":      {"in": 0.000075, "out": 0.0003},
    "gemini-2.5-pro":        {"in": 0.00125,  "out": 0.005},
    "gemini-2.5-flash-lite": {"in": 0.000035, "out": 0.00014},
    # Gemini 3.x family (currently configured default)
    "gemini-3.1-flash":      {"in": 0.000075, "out": 0.0003},
    "gemini-3.1-flash-lite": {"in": 0.000035, "out": 0.00014},
    # Anthropic Claude family
    "claude-opus-4-8":       {"in": 0.015,    "out": 0.075},
    "claude-opus-4-7":       {"in": 0.015,    "out": 0.075},
    "claude-sonnet-5":       {"in": 0.003,    "out": 0.015},
    "claude-fable-5":        {"in": 0.003,    "out": 0.015},
    "claude-haiku-4-5-20251001": {"in": 0.001, "out": 0.005},
    # OpenAI family
    "gpt-4o":                {"in": 0.0025,   "out": 0.01},
    "gpt-4o-mini":           {"in": 0.00015,  "out": 0.0006},
    "gpt-4-turbo":           {"in": 0.01,     "out": 0.03},
    # Local / OSS via Ollama — zero cost by definition.
    "llama3":                {"in": 0.0,      "out": 0.0},
    "llama3.1":              {"in": 0.0,      "out": 0.0},
    "qwen2.5":               {"in": 0.0,      "out": 0.0},
}


@dataclass
class TokenUsage:
    """Per-call token + cost record.

    ``cached_tokens_in`` is the subset of ``tokens_in`` that the provider
    reports as served from its context cache; ``compute_cost`` bills it at
    ``CACHE_RATE``. Tuple-style return (provider returns ``(text, usage)``) is
    preferred over a shared ``provider.last_usage`` attribute because multiple
    concurrent chats share the provider instance under the worker-thread model.
    """
    tokens_in: int = 0
    cached_tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    model: str = ""

    @classmethod
    def empty(cls, model: str = "") -> "TokenUsage":
        return cls(model=model)

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        """Accumulate usage across multiple calls in a single run."""
        if not isinstance(other, TokenUsage):
            return NotImplemented
        # Model is best-effort — fall back to whichever side reports one. In
        # mixed-model runs the per-step model is still recorded individually.
        merged_model = self.model or other.model
        return TokenUsage(
            tokens_in=self.tokens_in + other.tokens_in,
            cached_tokens_in=self.cached_tokens_in + other.cached_tokens_in,
            tokens_out=self.tokens_out + other.tokens_out,
            cost_usd=self.cost_usd + other.cost_usd,
            model=merged_model,
        )


def compute_cost(usage: TokenUsage) -> float:
    """Return the USD cost for ``usage`` given ``PRICE_PER_1K_TOKENS``.

    Unknown model -> log WARNING + return 0.0 so the missing-rate case is loud
    (the warning) but non-disruptive (cost still aggregates).
    """
    rates = PRICE_PER_1K_TOKENS.get(usage.model)
    if not rates:
        logger.warning("unpriced model: %s — cost reported as 0", usage.model)
        return 0.0
    fresh_in = max(usage.tokens_in - usage.cached_tokens_in, 0)
    return (
        fresh_in * rates["in"] / 1000
        + usage.cached_tokens_in * rates["in"] * CACHE_RATE / 1000
        + usage.tokens_out * rates["out"] / 1000
    )
