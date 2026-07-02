"""Tests for the in-process rate limiter."""

from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import ratelimit  # noqa: E402


def test_rate_limiter_prunes_stale_keys_when_cap_is_crossed(monkeypatch):
    limiter = ratelimit.RateLimiter()
    clock = {"now": 1000.0}
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: clock["now"])

    for i in range(4097):
        assert limiter.allow(f"ip-{i}", limit=1, window_seconds=10) is True

    clock["now"] = 2000.0
    assert limiter.allow("fresh-ip", limit=1, window_seconds=10) is True

    assert list(limiter._hits.keys()) == ["fresh-ip"]
