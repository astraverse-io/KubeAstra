"""Shared dataclasses for the summarizer module."""

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class SummaryStats:
    """Counters describing what the summarizer did. Surfaces as `summary_stats`
    on the tool's return dict so we can tune thresholds and audit cost."""

    bytes_in: int = 0
    bytes_out: int = 0
    lines_in: int = 0
    lines_out: int = 0
    duplicates_collapsed: int = 0
    error_lines: int = 0
    warn_lines: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SummaryResult:
    """Result of summarizing one tool output.

    Attributes:
        summary: The compacted text the LLM should see.
        method:  Provenance of the summary — "llm", "heuristic", or "none".
                 "none" means summarization was disabled or skipped (input
                 below threshold). In that case `summary` is empty and
                 the caller should fall back to the original raw text.
        stats:   Counters describing the work done.
    """

    summary: str = ""
    method: str = "none"          # "llm" | "heuristic" | "none"
    stats: SummaryStats = field(default_factory=SummaryStats)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "method": self.method,
            "stats": self.stats.to_dict(),
        }
