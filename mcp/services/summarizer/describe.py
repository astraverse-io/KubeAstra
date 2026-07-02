"""Summarizer for kubectl describe output (pods, deployments, etc.).

The full describe is structured by section headers ("Status:", "Conditions:",
"Containers:", "Events:", ...). For diagnosis the high-signal sections are
small; the long tail (Annotations, Tolerations, Volumes, QoS Class,
Node-Selectors, Topology Spread Constraints) almost never carries the
root cause. This summarizer keeps the former and drops the latter.

Strategy (heuristic, deterministic, no LLM):
  1. Split by top-level section headers (col-0 "Word: ...").
  2. Keep allowlisted sections verbatim.
  3. Drop blocklisted sections entirely.
  4. For any unrecognized section, keep only the first line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

from config.settings import get_settings


# Sections worth keeping in full.
_KEEP = frozenset({
    "Name", "Namespace", "Priority", "Status", "Reason", "Message",
    "Conditions", "Containers", "Init Containers", "Ephemeral Containers",
    "Controlled By", "Events",
})

# Sections to drop entirely.
_DROP = frozenset({
    "Annotations", "Labels", "Tolerations", "Volumes",
    "Node-Selectors", "Topology Spread Constraints", "QoS Class",
    "Service Account", "Restart Count", "IPs",
})

# A "section header" starts at column 0, ends with ":", and is followed by
# either content on the same line or indented continuation.
_SECTION_HEADER_RE = re.compile(r"^([A-Z][A-Za-z0-9 /\-]+):(\s*.*)$")


@dataclass
class DescribeSummaryStats:
    bytes_in: int = 0
    bytes_out: int = 0
    sections_kept: int = 0
    sections_dropped: int = 0


@dataclass
class DescribeSummaryResult:
    summary: str
    method: str  # "heuristic" | "none"
    stats: DescribeSummaryStats


def summarize_describe(raw: str) -> DescribeSummaryResult:
    settings = get_settings()
    stats = DescribeSummaryStats(bytes_in=len(raw.encode("utf-8")) if raw else 0)

    if not raw or not raw.strip():
        return DescribeSummaryResult("", "none", stats)

    if not settings.enable_log_summarization:
        return DescribeSummaryResult(raw, "none", stats)

    if stats.bytes_in < settings.log_summarization_threshold_bytes:
        stats.bytes_out = stats.bytes_in
        return DescribeSummaryResult(raw, "none", stats)

    kept_chunks: List[str] = []
    current_header: str | None = None
    current_body: List[str] = []

    def flush():
        nonlocal current_header, current_body
        if current_header is None:
            return
        if current_header in _DROP:
            stats.sections_dropped += 1
        elif current_header in _KEEP:
            kept_chunks.append(_render(current_header, current_body))
            stats.sections_kept += 1
        else:
            # Unknown — keep header + first non-empty line for breadcrumbs.
            first = next((ln for ln in current_body if ln.strip()), "")
            if first:
                kept_chunks.append(f"{current_header}: {first.strip()}")
            stats.sections_dropped += 1
        current_header = None
        current_body = []

    for line in raw.splitlines():
        m = _SECTION_HEADER_RE.match(line)
        if m and not line.startswith(" "):
            flush()
            current_header = m.group(1)
            rest = m.group(2)
            if rest.strip():
                current_body.append(rest)
        else:
            if current_header is None:
                # Pre-header preamble — usually empty; ignore.
                continue
            current_body.append(line)

    flush()

    summary = "\n".join(kept_chunks).strip()
    stats.bytes_out = len(summary.encode("utf-8"))
    return DescribeSummaryResult(summary, "heuristic", stats)


def _render(header: str, body: List[str]) -> str:
    body_text = "\n".join(body).rstrip()
    if body_text:
        return f"{header}:\n{body_text}"
    return f"{header}:"
