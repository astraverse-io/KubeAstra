"""Tests for the per-target circuit breaker.

Covers the state machine in §11 of the remote diagnostics plan: closed → open
after threshold within window, open → half_open after cooldown, half_open →
closed on probe success, half_open → open on probe failure. Also concurrent
access from multiple threads.
"""

from __future__ import annotations

import threading
import sys
from pathlib import Path

import pytest

from remote_diagnostics_breaker import RemoteCircuitBreaker


class _Clock:
    """Replaces time.monotonic so tests are deterministic."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _breaker(clock: _Clock, **overrides):
    b = RemoteCircuitBreaker(
        failure_threshold=overrides.get("failure_threshold", 3),
        failure_window_seconds=overrides.get("failure_window_seconds", 60.0),
        open_duration_seconds=overrides.get("open_duration_seconds", 300.0),
    )
    b._now = clock  # noqa: SLF001 — tests intentionally inject a fake clock
    return b


def test_closed_by_default_allows_traffic():
    clock = _Clock()
    b = _breaker(clock)
    decision = b.allow("qa17")
    assert decision.allowed is True
    assert decision.state == "closed"


def test_single_failure_does_not_open():
    clock = _Clock()
    b = _breaker(clock)
    b.record_failure("qa17", "connect_timeout")
    assert b.allow("qa17").allowed is True


def test_threshold_failures_within_window_open_the_circuit():
    clock = _Clock()
    b = _breaker(clock)
    b.record_failure("qa17", "connect_timeout")
    clock.advance(10)
    b.record_failure("qa17", "auth_failed")
    clock.advance(10)
    b.record_failure("qa17", "host_key_mismatch")
    decision = b.allow("qa17")
    assert decision.allowed is False
    assert decision.state == "open"
    assert decision.reason == "circuit_open"
    assert decision.retry_after_seconds is not None
    assert decision.retry_after_seconds > 0


def test_failures_outside_window_do_not_count():
    clock = _Clock()
    b = _breaker(clock, failure_window_seconds=60.0)
    b.record_failure("qa17", "connect_timeout")
    b.record_failure("qa17", "connect_timeout")
    clock.advance(120)  # window elapsed
    b.record_failure("qa17", "connect_timeout")
    # Only one failure remains in the window; circuit stays closed.
    assert b.allow("qa17").allowed is True


def test_open_circuit_blocks_other_targets_independently():
    clock = _Clock()
    b = _breaker(clock)
    for _ in range(3):
        b.record_failure("qa17", "connect_timeout")
    assert b.allow("qa17").allowed is False
    assert b.allow("qa18").allowed is True  # different target unaffected


def test_open_circuit_transitions_to_half_open_after_cooldown():
    clock = _Clock()
    b = _breaker(clock, open_duration_seconds=300.0)
    for _ in range(3):
        b.record_failure("qa17", "connect_timeout")
    assert b.allow("qa17").state == "open"
    clock.advance(301)
    decision = b.allow("qa17")
    assert decision.allowed is True
    assert decision.state == "half_open"


def test_half_open_allows_only_one_probe():
    clock = _Clock()
    b = _breaker(clock, open_duration_seconds=300.0)
    for _ in range(3):
        b.record_failure("qa17", "connect_timeout")
    clock.advance(301)
    first = b.allow("qa17")
    second = b.allow("qa17")
    assert first.allowed is True
    assert second.allowed is False
    assert second.state == "half_open"


def test_probe_success_closes_the_circuit():
    clock = _Clock()
    b = _breaker(clock, open_duration_seconds=300.0)
    for _ in range(3):
        b.record_failure("qa17", "connect_timeout")
    clock.advance(301)
    probe = b.allow("qa17")  # consumes the probe slot
    assert probe.probe_token is not None
    b.record_success("qa17", probe_token=probe.probe_token)
    decision = b.allow("qa17")
    assert decision.allowed is True
    assert decision.state == "closed"


def test_probe_failure_re_opens_with_fresh_cooldown():
    clock = _Clock()
    b = _breaker(clock, open_duration_seconds=300.0)
    for _ in range(3):
        b.record_failure("qa17", "connect_timeout")
    clock.advance(301)
    probe = b.allow("qa17")
    assert probe.probe_token is not None
    b.record_failure("qa17", "connect_timeout", probe_token=probe.probe_token)
    decision = b.allow("qa17")
    assert decision.allowed is False
    assert decision.state == "open"
    # The cooldown resets — close to full 300 s remaining
    assert decision.retry_after_seconds is not None
    assert decision.retry_after_seconds > 290


def test_snapshot_reports_current_state_and_count():
    clock = _Clock()
    b = _breaker(clock)
    state, count = b.snapshot("qa17")
    assert state == "closed"
    assert count == 0
    b.record_failure("qa17", "connect_timeout")
    state, count = b.snapshot("qa17")
    assert state == "closed"
    assert count == 1


def test_invalid_configuration_rejected():
    with pytest.raises(ValueError):
        RemoteCircuitBreaker(failure_threshold=0)
    with pytest.raises(ValueError):
        RemoteCircuitBreaker(failure_window_seconds=0)
    with pytest.raises(ValueError):
        RemoteCircuitBreaker(open_duration_seconds=-1)


def test_concurrent_failure_recording_is_thread_safe():
    """Two threads each pushing 5 failures should not corrupt the deque, and
    the resulting state must be deterministically 'open'."""
    b = RemoteCircuitBreaker(failure_threshold=3, failure_window_seconds=60.0)

    def push() -> None:
        for _ in range(5):
            b.record_failure("qa17", "connect_timeout")

    threads = [threading.Thread(target=push) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    state, count = b.snapshot("qa17")
    assert state == "open"
    # Window is 60s, all 10 happened immediately → all retained
    assert count == 10


def test_states_returns_all_known_targets():
    clock = _Clock()
    b = _breaker(clock)
    b.allow("qa17")
    b.allow("qa18")
    states = b.states()
    assert states == {"qa17": "closed", "qa18": "closed"}


def test_record_failure_rejects_unknown_reason():
    """P2.2: Literal is a static check only. record_failure validates the
    reason at runtime so a typo or arbitrary string never silently counts."""
    from remote_diagnostics_breaker import RemoteCircuitBreaker
    b = RemoteCircuitBreaker()
    with pytest.raises(ValueError, match="not a breaker failure category"):
        b.record_failure("qa17", "kerblam")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        b.record_failure("qa17", "")  # type: ignore[arg-type]


def test_record_failure_rejects_upstream_rejection_reasons():
    """``circuit_open``, ``target_disabled``, and ``unauthorized`` are metric
    labels for the caller to emit but never inputs to the breaker — those
    cases mean SSH was not even attempted."""
    from remote_diagnostics_breaker import RemoteCircuitBreaker
    b = RemoteCircuitBreaker()
    for reason in ("circuit_open", "target_disabled", "unauthorized"):
        with pytest.raises(ValueError):
            b.record_failure("qa17", reason)  # type: ignore[arg-type]


def test_failure_reason_constants_are_consistent_with_plan_metric_enum():
    """BREAKER_FAILURE_REASONS must be a strict subset of
    CONNECTION_ATTEMPT_REASONS; the difference is exactly the upstream
    rejections enumerated in plan §14."""
    from remote_diagnostics_breaker import (
        BREAKER_FAILURE_REASONS,
        CONNECTION_ATTEMPT_REASONS,
    )
    assert BREAKER_FAILURE_REASONS < CONNECTION_ATTEMPT_REASONS
    assert CONNECTION_ATTEMPT_REASONS - BREAKER_FAILURE_REASONS == {
        "circuit_open", "target_disabled", "unauthorized",
    }


def test_breaker_reasons_match_ssh_runner_taxonomy():
    """The deployable packages stay self-contained, so enforce parity in CI."""
    from remote_diagnostics_breaker import BREAKER_FAILURE_REASONS

    mcp_dir = Path(__file__).resolve().parents[3] / "mcp"
    if str(mcp_dir) not in sys.path:
        sys.path.insert(0, str(mcp_dir))
    from k8s.remote_connection import REMOTE_CONNECTION_REASONS

    assert BREAKER_FAILURE_REASONS == REMOTE_CONNECTION_REASONS


def test_abandoned_probe_re_opens_after_lease():
    """P2: a half-open probe whose worker crashes must not wedge the breaker
    forever. After the lease elapses, the next allow() call observes the
    target as re-opened, not stuck in half-open."""
    clock = _Clock()
    b = _breaker(clock, open_duration_seconds=300.0)
    b._probe_lease = 120.0  # override for the test
    for _ in range(3):
        b.record_failure("qa17", "connect_timeout")
    clock.advance(301)
    first = b.allow("qa17")
    assert first.allowed is True
    assert first.state == "half_open"
    # Probe is now in flight. Simulate the worker crashing — no
    # record_success / record_failure is ever called. After the lease
    # elapses, the breaker must recover.
    clock.advance(121)
    after = b.allow("qa17")
    assert after.allowed is False
    assert after.state == "open"
    assert after.reason == "circuit_open"
    # And the next caller does not block forever — once the new cooldown
    # elapses they can probe again.
    clock.advance(301)
    retry = b.allow("qa17")
    assert retry.allowed is True
    assert retry.state == "half_open"


def test_probe_within_lease_still_blocks_concurrent_callers():
    """Lease only triggers after the deadline. While the probe is
    legitimately in flight, other callers still see half-open and are
    rejected — the abandonment check must not kick in early."""
    clock = _Clock()
    b = _breaker(clock, open_duration_seconds=300.0)
    b._probe_lease = 120.0
    for _ in range(3):
        b.record_failure("qa17", "connect_timeout")
    clock.advance(301)
    first = b.allow("qa17")
    assert first.allowed is True
    clock.advance(30)  # well within the 120 s lease
    second = b.allow("qa17")
    assert second.allowed is False
    assert second.state == "half_open"


def test_record_success_clears_probe_started_at():
    """A clean probe success leaves no leftover lease state that could later
    spuriously expire a fresh half-open."""
    clock = _Clock()
    b = _breaker(clock, open_duration_seconds=300.0)
    b._probe_lease = 120.0
    for _ in range(3):
        b.record_failure("qa17", "connect_timeout")
    clock.advance(301)
    probe = b.allow("qa17")  # admit probe
    b.record_success("qa17", probe_token=probe.probe_token)
    # Long time passes; no probe is actually in flight any more.
    clock.advance(9999)
    decision = b.allow("qa17")
    assert decision.allowed is True
    assert decision.state == "closed"


# ── P2 race: stale probe completion cannot overwrite newer state ──────────────


def test_closed_admission_returns_no_probe_token():
    """Tokens are scoped to half-open admissions; closed-state callers do not
    own the breaker's exclusivity and must not receive a token."""
    clock = _Clock()
    b = _breaker(clock)
    decision = b.allow("qa17")
    assert decision.state == "closed"
    assert decision.probe_token is None


def test_half_open_admission_returns_unique_probe_token():
    clock = _Clock()
    b = _breaker(clock, open_duration_seconds=300.0)
    for _ in range(3):
        b.record_failure("qa17", "connect_timeout")
    clock.advance(301)
    probe1 = b.allow("qa17")
    assert probe1.state == "half_open"
    assert probe1.probe_token is not None
    b.record_failure("qa17", "connect_timeout", probe_token=probe1.probe_token)
    # New cooldown, new probe
    clock.advance(301)
    probe2 = b.allow("qa17")
    assert probe2.probe_token is not None
    assert probe2.probe_token != probe1.probe_token


def test_stale_success_after_abandonment_does_not_close_circuit():
    """Probe admitted, worker hangs past lease, breaker re-opens. The
    original worker eventually returns and calls record_success with its
    stale token — the breaker MUST stay open."""
    clock = _Clock()
    b = _breaker(clock, open_duration_seconds=300.0)
    b._probe_lease = 120.0
    for _ in range(3):
        b.record_failure("qa17", "connect_timeout")
    clock.advance(301)
    stale = b.allow("qa17")
    assert stale.probe_token is not None
    clock.advance(121)  # lease expires; next allow() reopens
    re_opened = b.allow("qa17")
    assert re_opened.state == "open"
    # Now the abandoned worker returns. It still has the original token.
    b.record_success("qa17", probe_token=stale.probe_token)
    after = b.allow("qa17")
    assert after.allowed is False
    assert after.state == "open"


def test_stale_failure_after_abandonment_is_ignored():
    """Symmetric to the success case — a stale record_failure must not
    re-open with a fresh cooldown either; the breaker should reflect only
    the abandonment-driven re-open."""
    clock = _Clock()
    b = _breaker(clock, open_duration_seconds=300.0)
    b._probe_lease = 120.0
    for _ in range(3):
        b.record_failure("qa17", "connect_timeout")
    clock.advance(301)
    stale = b.allow("qa17")
    clock.advance(121)
    b.allow("qa17")  # re-opens because of abandonment
    # Capture opened_at by checking retry_after — should be close to 300s
    decision_before = b.allow("qa17")
    assert decision_before.state == "open"
    retry_before = decision_before.retry_after_seconds
    # Late failure from the abandoned worker.
    b.record_failure("qa17", "connect_timeout", probe_token=stale.probe_token)
    decision_after = b.allow("qa17")
    assert decision_after.state == "open"
    # opened_at did not jump forward; retry_after_seconds is unchanged
    # (the stale call did not reset the cooldown).
    assert decision_after.retry_after_seconds == retry_before


def test_stale_success_does_not_resolve_replacement_probe():
    """After abandonment, a replacement probe is admitted with a fresh
    token. The original probe's late record_success must not close the
    breaker on behalf of the new probe."""
    clock = _Clock()
    b = _breaker(clock, open_duration_seconds=300.0)
    b._probe_lease = 120.0
    for _ in range(3):
        b.record_failure("qa17", "connect_timeout")
    clock.advance(301)
    abandoned = b.allow("qa17")
    clock.advance(121)
    # Abandonment expires the probe; new cooldown begins.
    b.allow("qa17")  # observed as "open"
    clock.advance(301)
    fresh = b.allow("qa17")
    assert fresh.state == "half_open"
    assert fresh.probe_token is not None
    assert fresh.probe_token != abandoned.probe_token
    # The original worker now returns successful. It must NOT close
    # the breaker on behalf of the fresh probe.
    b.record_success("qa17", probe_token=abandoned.probe_token)
    state_now, _ = b.snapshot("qa17")
    assert state_now == "half_open"
    # The fresh probe completing legitimately still closes the breaker.
    b.record_success("qa17", probe_token=fresh.probe_token)
    final, _ = b.snapshot("qa17")
    assert final == "closed"


def test_stale_failure_does_not_re_open_during_replacement_probe():
    """Mirror of the success case for failure path."""
    clock = _Clock()
    b = _breaker(clock, open_duration_seconds=300.0)
    b._probe_lease = 120.0
    for _ in range(3):
        b.record_failure("qa17", "connect_timeout")
    clock.advance(301)
    abandoned = b.allow("qa17")
    clock.advance(121)
    b.allow("qa17")  # re-opens
    clock.advance(301)
    fresh = b.allow("qa17")
    assert fresh.state == "half_open"
    # Late failure from the original probe arrives. The breaker should
    # remain in half_open with the fresh probe still in flight.
    b.record_failure("qa17", "connect_timeout", probe_token=abandoned.probe_token)
    state_now, _ = b.snapshot("qa17")
    assert state_now == "half_open"


def test_record_without_token_is_ignored_while_probe_in_flight():
    """A closed-state worker (no token) completing while a probe is in
    flight must not close or re-open the breaker. The probe owns the state
    transition."""
    clock = _Clock()
    b = _breaker(clock, open_duration_seconds=300.0)
    for _ in range(3):
        b.record_failure("qa17", "connect_timeout")
    clock.advance(301)
    b.allow("qa17")  # probe admitted; we don't keep the token
    # No-token record_success arrives (simulating a late closed-state caller).
    b.record_success("qa17")
    state, _ = b.snapshot("qa17")
    assert state == "half_open"
    b.record_failure("qa17", "connect_timeout")
    state, _ = b.snapshot("qa17")
    assert state == "half_open"


def test_closed_state_record_success_still_works():
    """Token enforcement only kicks in during half-open. Closed-state
    record_success and record_failure remain backward-compatible — callers
    that don't pass a token can still drain the failure history on success
    or contribute to it on failure."""
    clock = _Clock()
    b = _breaker(clock)
    b.record_failure("qa17", "connect_timeout")
    b.record_failure("qa17", "connect_timeout")
    state, count = b.snapshot("qa17")
    assert state == "closed"
    assert count == 2
    # Closed-state success clears the failure history.
    b.record_success("qa17")
    state, count = b.snapshot("qa17")
    assert state == "closed"
    assert count == 0


def test_probe_lease_validation():
    with pytest.raises(ValueError):
        RemoteCircuitBreaker(probe_lease_seconds=0)
    with pytest.raises(ValueError):
        RemoteCircuitBreaker(probe_lease_seconds=-1)
