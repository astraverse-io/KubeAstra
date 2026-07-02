"""Heuristic + optional-LLM summarizer for pod log output.

Pipeline:
  1. Heuristic cleanup (always runs, deterministic, free):
       - Strip ANSI escape sequences.
       - Drop blank / whitespace-only lines.
       - Collapse runs of identical lines into "<line> (xN)".
       - Tag lines that look like errors/warnings/stack frames.
       - Keep head + tail context, plus every tagged line.
  2. Optional LLM polish (if enabled and provider available):
       - Pass the cleaned text to the configured LLM provider for a
         tight prose summary.
       - On any failure, fall back to the heuristic output.

The heuristic alone typically cuts 70–90 % of noise on a busy pod and
is useful even without an LLM available.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from config.settings import get_settings

logger = logging.getLogger(__name__)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_ERROR_KEYWORDS = (
    "error", "err ", "fatal", "panic", "exception", "traceback",
    "fail", "crash", "killed", "oom", "denied", "refused", "timeout",
    "unauthorized", "forbidden",
)
_WARN_KEYWORDS = ("warn", "warning", "deprecated", "retry")
_STACK_FRAME_RE = re.compile(r"^\s*(at\s|\s{2,}File\s|Traceback|\.\.\.\s\d+\smore)")

# Head / tail context to keep around tagged lines, regardless of dedup.
_HEAD_LINES = 5
_TAIL_LINES = 10
# Cap on lines we'll feed to the LLM (after dedup + tagging). Prevents
# pathological inputs from blowing the prompt budget.
_MAX_LINES_TO_LLM = 120


@dataclass
class SummaryStats:
    bytes_in: int = 0
    bytes_out: int = 0
    lines_in: int = 0
    lines_out: int = 0
    duplicates_collapsed: int = 0
    error_lines: int = 0
    warn_lines: int = 0


@dataclass
class SummaryResult:
    summary: str
    method: str  # "llm" | "heuristic" | "none"
    stats: SummaryStats = field(default_factory=SummaryStats)


def summarize_logs(text: str) -> SummaryResult:
    """Summarize pod log output for LLM consumption.

    Returns SummaryResult; callers should treat the original text as the
    canonical UI artifact and use `summary` only for LLM context.
    """
    settings = get_settings()

    if not text or not text.strip():
        return SummaryResult(summary="", method="none")

    bytes_in = len(text.encode("utf-8"))

    # Skip work for small outputs — the threshold gate prevents wasted LLM
    # calls on already-tight inputs.
    if bytes_in < settings.log_summarization_threshold_bytes:
        return SummaryResult(
            summary=text,
            method="none",
            stats=SummaryStats(bytes_in=bytes_in, bytes_out=bytes_in,
                               lines_in=text.count("\n") + 1,
                               lines_out=text.count("\n") + 1),
        )

    cleaned_text, stats = _heuristic_clean(text)
    stats.bytes_in = bytes_in

    if settings.log_summarization_use_llm:
        polished = _llm_polish(cleaned_text, stats)
        if polished is not None:
            stats.bytes_out = len(polished.encode("utf-8"))
            return SummaryResult(summary=polished, method="llm", stats=stats)

    stats.bytes_out = len(cleaned_text.encode("utf-8"))
    return SummaryResult(summary=cleaned_text, method="heuristic", stats=stats)


# ── Heuristic stage ──────────────────────────────────────────────────────────

def _heuristic_clean(text: str) -> tuple[str, SummaryStats]:
    stats = SummaryStats()

    raw_lines = text.splitlines()
    stats.lines_in = len(raw_lines)

    cleaned: List[str] = []
    prev: Optional[str] = None
    dup_run = 0

    for raw in raw_lines:
        line = _ANSI_RE.sub("", raw).rstrip()
        if not line.strip():
            continue

        if line == prev:
            dup_run += 1
            stats.duplicates_collapsed += 1
            continue

        if dup_run > 0:
            cleaned[-1] = f"{cleaned[-1]}  (xN={dup_run + 1})"
            dup_run = 0

        cleaned.append(line)
        prev = line

    if dup_run > 0 and cleaned:
        cleaned[-1] = f"{cleaned[-1]}  (xN={dup_run + 1})"

    # Tag lines of interest.
    tagged_idx = set()
    for i, line in enumerate(cleaned):
        lower = line.lower()
        if any(k in lower for k in _ERROR_KEYWORDS) or _STACK_FRAME_RE.match(line):
            tagged_idx.add(i)
            stats.error_lines += 1
        elif any(k in lower for k in _WARN_KEYWORDS):
            tagged_idx.add(i)
            stats.warn_lines += 1

    # Keep head, tail, and every tagged line (with ±1 line context).
    keep_idx = set(range(min(_HEAD_LINES, len(cleaned))))
    keep_idx |= set(range(max(0, len(cleaned) - _TAIL_LINES), len(cleaned)))
    for i in tagged_idx:
        keep_idx.add(i)
        if i > 0:
            keep_idx.add(i - 1)
        if i + 1 < len(cleaned):
            keep_idx.add(i + 1)

    selected = [cleaned[i] for i in sorted(keep_idx)]

    # Cap before LLM. Prefer to drop from the middle, keep head + tail intact.
    if len(selected) > _MAX_LINES_TO_LLM:
        head = selected[: _MAX_LINES_TO_LLM // 2]
        tail = selected[-_MAX_LINES_TO_LLM // 2:]
        selected = head + [f"… [{len(selected) - _MAX_LINES_TO_LLM} lines elided] …"] + tail

    stats.lines_out = len(selected)
    return "\n".join(selected), stats


# ── LLM polish stage ─────────────────────────────────────────────────────────

_SUMMARY_SYSTEM = (
    "You summarize Kubernetes pod logs for an automated investigation pipeline. "
    "Be terse, factual, and never invent details not present in the input. "
    "If the logs look healthy or empty, say so explicitly."
)

_SUMMARY_PROMPT_TEMPLATE = """Summarize the following pod logs for a DevOps engineer.

Output format (strict):
- A single short paragraph (≤ 3 sentences) describing what the logs indicate.
- Then a bulleted list (max 8 bullets) of unique error/warning signatures
  observed, each prefixed with its severity in square brackets: [ERROR] or [WARN].
- Do not echo raw stack traces; reference them by name (e.g., "NullPointerException in handler.process").
- If no errors or warnings are present, omit the bullet list.

Logs (already deduplicated and filtered):
---
{logs}
---"""


def _llm_polish(cleaned_text: str, stats: SummaryStats) -> Optional[str]:
    """Run the cleaned text through the configured LLM provider.

    Returns the polished summary on success, or None on any failure (in
    which case the caller falls back to the heuristic output).
    """
    settings = get_settings()
    try:
        from services.llm import get_provider
    except Exception as exc:
        logger.debug("Log summarizer: LLM provider import failed: %s", exc)
        return None

    try:
        provider = get_provider()
    except Exception as exc:
        logger.debug("Log summarizer: provider init failed: %s", exc)
        return None

    if not getattr(provider, "enabled", False):
        return None

    try:
        prompt = _SUMMARY_PROMPT_TEMPLATE.format(logs=cleaned_text)
        return provider.generate(
            prompt=prompt,
            system=_SUMMARY_SYSTEM,
            temperature=0.1,
            max_tokens=settings.log_summarization_max_tokens,
        ).strip() or None
    except Exception as exc:
        logger.warning("Log summarizer: LLM polish failed (%s); falling back to heuristic", exc)
        return None
