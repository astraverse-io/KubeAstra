"""Phase 2.3 — Semantic prompt cache (L2).

Before invoking the retrieval router or the LLM, check whether a similar
question was answered in the recent past. If yes (similarity above a
strict threshold AND within the lookback window), return that prior
answer instantly — **zero LLM call, zero tool calls, ~50 ms total**.

Distinguishes itself from Phase 1.4's "cached" mode:
  - 1.4 cached: requires ``verified=True`` (human 👍'd) AND searches
    the ``runbook`` collection. Catches recurring problems the team
    has explicitly endorsed.
  - 2.3 cache: tighter similarity (0.95 vs 0.92), no human verification
    needed, but bounded to the last N hours. Catches paraphrases of
    questions a teammate JUST asked within the working day.

Reuses, by design:
  - Qdrant (no new infrastructure)
  - The ``session_memory`` collection populated by Phase 1.3 capture
  - The existing embedding model loaded by ``services.embeddings``

If the feature is disabled, the embedding model isn't loaded by this
module — ``lookup`` returns immediately.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


def lookup(question: str) -> Tuple[Optional[dict], float]:
    """Search ``session_memory`` for a prior similar question.

    Returns:
        ``(payload, similarity)`` on hit — payload includes the original
        question, resolution, user, timestamp, etc.
        ``(None, 0.0)`` on miss for any reason: feature disabled, empty
        question, vector DB unreachable, collection doesn't exist, no
        match above threshold, or no match within the recency window.

    Never raises. Failures degrade silently to "no hit" so a broken
    cache never breaks chat.
    """
    if not (question or "").strip():
        return None, 0.0

    try:
        from config.settings import get_settings
        settings = get_settings()
    except Exception as exc:
        logger.debug("prompt_cache: settings load failed: %s", exc)
        return None, 0.0

    if not getattr(settings, "prompt_cache_enabled", False):
        return None, 0.0

    threshold = float(getattr(settings, "prompt_cache_threshold", 0.95))
    lookback_hours = int(getattr(settings, "prompt_cache_lookback_hours", 24))
    top_k = int(getattr(settings, "prompt_cache_top_k", 5))

    try:
        from services.embeddings import embeddings
        from services.rag.schema import SESSION_MEMORY
        from services.vector_db import vector_db
    except Exception as exc:
        logger.debug("prompt_cache: imports failed: %s", exc)
        return None, 0.0

    try:
        vector_db.connect()
    except Exception as exc:
        logger.debug("prompt_cache: vector DB unavailable: %s", exc)
        return None, 0.0

    try:
        qvec = embeddings.embed(question)
    except Exception as exc:
        logger.debug("prompt_cache: embed failed: %s", exc)
        return None, 0.0

    try:
        hits = vector_db.search_in(
            collection=SESSION_MEMORY.name,
            query_vector=qvec,
            limit=top_k,
        )
    except Exception as exc:
        # 404 collection-doesn't-exist is the common case before
        # session_memory is first written to. Silent skip.
        logger.debug("prompt_cache: search_in failed: %s", exc)
        return None, 0.0

    if not hits:
        return None, 0.0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    # Hits are sorted by similarity desc. Find the first one that
    # clears BOTH the similarity bar AND the recency filter.
    for hit in hits:
        sim = float(hit.get("similarity") or 0.0)
        if sim < threshold:
            # No subsequent hit can clear the bar — they're only less similar.
            break
        ts = _parse_iso(hit.get("timestamp"))
        if ts is None:
            # Conservative: don't return a hit we can't recency-check.
            continue
        if ts < cutoff:
            # Too old. Keep scanning in case there's a slightly-less-similar
            # hit that's recent enough.
            continue
        return hit, sim

    return None, 0.0


def format_cached_answer(payload: dict) -> str:
    """Render a cached session_memory payload as a markdown answer with
    a one-line attribution header so users can tell the difference from
    a fresh LLM-generated reply."""
    resolution = (payload.get("resolution") or "").strip()
    question = (payload.get("question") or "").strip()
    user = payload.get("user") or "a teammate"
    ts = _parse_iso(payload.get("timestamp"))

    age_str = ""
    if ts:
        delta = datetime.now(timezone.utc) - ts
        mins = max(0, int(delta.total_seconds() // 60))
        if mins < 1:
            age_str = "just now"
        elif mins < 60:
            age_str = f"{mins} min ago"
        else:
            hrs = mins // 60
            age_str = f"{hrs} hour{'s' if hrs != 1 else ''} ago"

    attribution_parts = []
    if user and user not in ("anonymous", "system"):
        attribution_parts.append(f"by `{user}`")
    if age_str:
        attribution_parts.append(age_str)
    attribution = " ".join(attribution_parts) or "earlier today"

    lines: list[str] = [
        f"💡 **Cached answer** — a similar question was answered {attribution}.",
        "",
    ]
    if question:
        snippet = question if len(question) <= 160 else question[:157] + "..."
        lines.append(f"> Earlier question: _{snippet}_")
        lines.append("")
    if resolution:
        lines.append(resolution)
    else:
        lines.append("_(no resolution text in cache — fall back to fresh investigation)_")
    return "\n".join(lines)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    """Parse ISO 8601 string into a timezone-aware datetime, or None
    if unparseable. Naive datetimes are assumed UTC."""
    if not s or not isinstance(s, str):
        return None
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None
