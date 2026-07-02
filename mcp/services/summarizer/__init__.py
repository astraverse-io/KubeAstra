"""Tool result summarization.

Reduces large kubectl tool outputs (logs, events, describe) to a tight
summary suitable for LLM context, while the raw output remains available
for the UI.

Public API:
    summarize_logs(text)        -> SummaryResult
    summarize_events(list)      -> EventsSummaryResult
    summarize_describe(text)    -> DescribeSummaryResult
"""

from .logs import summarize_logs, SummaryResult
from .events import summarize_events, EventsSummaryResult
from .describe import summarize_describe, DescribeSummaryResult

__all__ = [
    "summarize_logs", "SummaryResult",
    "summarize_events", "EventsSummaryResult",
    "summarize_describe", "DescribeSummaryResult",
]
