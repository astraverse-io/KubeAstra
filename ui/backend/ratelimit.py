"""Minimal in-process sliding-window rate limiter.

Per-process only (not shared across workers) — a first line of defense against
forgot/reset abuse, not a distributed limiter. For multi-worker/HA, back this
with Redis or a shared store later.
"""
from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: float) -> bool:
        """Record an attempt for ``key``; return False if it exceeds ``limit``
        within ``window_seconds`` (sliding window)."""
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            bucket = [t for t in self._hits.get(key, ()) if t > cutoff]
            if len(bucket) >= limit:
                self._hits[key] = bucket
                return False
            bucket.append(now)
            self._hits[key] = bucket
            if len(self._hits) > 4096:  # opportunistic cleanup of stale keys
                pruned: dict[str, list[float]] = {}
                for k, values in self._hits.items():
                    fresh = [t for t in values if t > cutoff]
                    if fresh:
                        pruned[k] = fresh
                self._hits = pruned
            return True

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()
