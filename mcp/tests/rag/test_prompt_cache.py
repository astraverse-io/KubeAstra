"""Phase 2.3 — prompt cache unit tests.

Covers the lookup decision logic (similarity gate, recency gate,
disabled-flag, missing collection, defensive parsing). Does not exercise
the embedding model or live Qdrant — both are stubbed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _hit(similarity: float, *, age_minutes: int, question: str = "test q",
         resolution: str = "test r", user: str = "alice") -> dict:
    """Construct a session_memory-shaped hit dict."""
    ts = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return {
        "similarity": similarity,
        "timestamp": _iso(ts),
        "question": question,
        "resolution": resolution,
        "user": user,
    }


# ── lookup() ────────────────────────────────────────────────────────────────

def test_disabled_returns_miss():
    from services.rag import prompt_cache
    from config import settings as _settings_mod

    class _S:
        prompt_cache_enabled = False

    with patch.object(_settings_mod, "get_settings", return_value=_S()):
        hit, sim = prompt_cache.lookup("anything")
    assert hit is None and sim == 0.0


def test_empty_question_returns_miss():
    from services.rag import prompt_cache
    for q in ("", "   ", None):
        hit, sim = prompt_cache.lookup(q)
        assert hit is None and sim == 0.0


def _patched_lookup(question: str, hits: list[dict], *,
                    threshold: float = 0.95, lookback_hours: int = 24):
    """Helper: run lookup() with the embeddings + vector_db stubbed
    out and the requested settings injected.

    Critical: pre-import vector_db / embeddings / settings BEFORE the
    patch context — their module-level code reads real settings (e.g.
    ``vector_db = VectorDB()`` at import time accesses
    settings.qdrant_collection). Pre-importing ensures that module init
    runs with real settings; we only stub the symbols at call time.
    """
    # Pre-import (idempotent after first call) so module-level
    # initialization uses real settings, not our stub.
    from services.rag import prompt_cache  # noqa: F401
    from services import embeddings as _embeds_mod
    from services import vector_db as _vdb_mod
    from config import settings as _settings_mod

    class _Settings:
        prompt_cache_enabled = True
        prompt_cache_threshold = threshold
        prompt_cache_lookback_hours = lookback_hours
        prompt_cache_top_k = 5

    class _Embed:
        def embed(self, text):
            return [0.0] * 384

    class _Vdb:
        def connect(self): pass
        def search_in(self, **kwargs): return hits

    with patch.object(_settings_mod, "get_settings", return_value=_Settings()), \
         patch.object(_embeds_mod, "embeddings", _Embed()), \
         patch.object(_vdb_mod, "vector_db", _Vdb()):
        return prompt_cache.lookup(question)


def test_hit_above_threshold_and_within_window():
    hits = [_hit(0.97, age_minutes=15)]
    hit, sim = _patched_lookup("paraphrase", hits)
    assert hit is not None
    assert sim == 0.97


def test_miss_below_threshold():
    hits = [_hit(0.90, age_minutes=5)]
    hit, sim = _patched_lookup("paraphrase", hits)
    assert hit is None and sim == 0.0


def test_miss_outside_lookback_window():
    # Hit is similar enough but older than the 24h window
    hits = [_hit(0.99, age_minutes=60 * 25)]  # 25h ago
    hit, sim = _patched_lookup("paraphrase", hits, lookback_hours=24)
    assert hit is None and sim == 0.0


def test_picks_recent_when_first_is_stale():
    # Top hit is stale; second hit is fresh AND still above threshold
    hits = [
        _hit(0.99, age_minutes=60 * 30),  # 30h, too old
        _hit(0.96, age_minutes=10),       # fresh + above threshold
    ]
    hit, sim = _patched_lookup("paraphrase", hits, lookback_hours=24)
    assert hit is not None
    assert sim == 0.96


def test_breaks_when_first_below_threshold():
    # Hits are sorted desc; if first is below threshold, no need to scan more
    hits = [
        _hit(0.50, age_minutes=10),
        _hit(0.40, age_minutes=5),
    ]
    hit, sim = _patched_lookup("paraphrase", hits, threshold=0.95)
    assert hit is None and sim == 0.0


def test_unparseable_timestamp_is_skipped_not_returned():
    hits = [{"similarity": 0.99, "timestamp": "not-a-date",
             "question": "q", "resolution": "r", "user": "u"}]
    hit, sim = _patched_lookup("paraphrase", hits)
    assert hit is None and sim == 0.0


def test_empty_hits_returns_miss():
    hit, sim = _patched_lookup("paraphrase", [])
    assert hit is None and sim == 0.0


# ── format_cached_answer() ──────────────────────────────────────────────────

def test_format_cached_answer_includes_attribution_and_question():
    from services.rag.prompt_cache import format_cached_answer
    payload = {
        "question": "how do I scale my deployment?",
        "resolution": "Use kubectl scale --replicas=N deploy/<name>",
        "user": "alice",
        "timestamp": _iso(datetime.now(timezone.utc) - timedelta(minutes=10)),
    }
    md = format_cached_answer(payload)
    assert "Cached answer" in md
    assert "alice" in md
    assert "10 min ago" in md
    assert "how do I scale" in md
    assert "kubectl scale" in md


def test_format_cached_answer_handles_old_payload():
    from services.rag.prompt_cache import format_cached_answer
    payload = {
        "question": "q",
        "resolution": "r",
        "user": "bob",
        "timestamp": _iso(datetime.now(timezone.utc) - timedelta(hours=3)),
    }
    md = format_cached_answer(payload)
    assert "3 hour" in md  # "3 hours ago" or "3 hour ago"


def test_format_cached_answer_handles_missing_user():
    from services.rag.prompt_cache import format_cached_answer
    md = format_cached_answer({"resolution": "r", "user": "anonymous", "timestamp": _iso(datetime.now(timezone.utc))})
    assert "anonymous" not in md  # filtered out
    # Should still produce some attribution
    assert "Cached answer" in md


def test_format_cached_answer_truncates_long_question():
    from services.rag.prompt_cache import format_cached_answer
    long_q = "X" * 500
    md = format_cached_answer({
        "question": long_q,
        "resolution": "r",
        "user": "u",
        "timestamp": _iso(datetime.now(timezone.utc)),
    })
    # Should be truncated to ~160 chars with "..."
    assert "X" * 500 not in md
    assert "..." in md
