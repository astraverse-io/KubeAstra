"""Per-target circuit breaker for remote diagnostic SSH connections.

Implements §11 of the Ansible remote diagnostics plan. Keyed by ``target_id``,
thread-safe, in-process only. Records connection-phase failures (network,
authentication, host-key, identity-verification) and opens the circuit after
``failure_threshold`` failures within ``failure_window_seconds``. Stays open for
``open_duration_seconds``, then permits one half-open probe.

Plan caveat: per-process state. With multiple replicas or worker processes,
each has its own breaker. A shared-store (Redis) breaker is deferred to v2.

Successful kubectl results that simply found no resources are NOT failures —
only the connection-phase outcomes the runner reports as ``record_failure``
count toward the threshold.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

# Full set of reasons that may appear on the
# agent_remote_connection_attempts_total{reason=...} metric label (plan §14).
# This includes upstream rejections (circuit_open, target_disabled, unauthorized)
# that are NOT actual connection-phase failures and therefore must not be
# accepted by record_failure.
CONNECTION_ATTEMPT_REASONS: frozenset[str] = frozenset({
    "connect_timeout",
    "auth_failed",
    "host_key_mismatch",
    "host_key_missing",
    "identity_mismatch",
    "bastion_failed",
    "transport_error",
    "circuit_open",
    "target_disabled",
    "unauthorized",
})

# Subset accepted by ``record_failure``. These are the categories that
# represent an actual SSH-or-preflight failure and should count toward
# opening the breaker. Upstream rejections (circuit_open, target_disabled,
# unauthorized) are emitted as metric labels by the caller but never reach
# the breaker — the request was rejected before SSH was attempted.
BREAKER_FAILURE_REASONS: frozenset[str] = frozenset({
    "connect_timeout",
    "auth_failed",
    "host_key_mismatch",
    "host_key_missing",
    "identity_mismatch",
    "bastion_failed",
    "transport_error",
})

# Type-checker convenience. The actual contract is enforced at runtime against
# BREAKER_FAILURE_REASONS — Literal alone would be silently bypassable.
FailureReason = Literal[
    "connect_timeout",
    "auth_failed",
    "host_key_mismatch",
    "host_key_missing",
    "identity_mismatch",
    "bastion_failed",
    "transport_error",
]

State = Literal["closed", "open", "half_open"]


@dataclass(frozen=True, slots=True)
class BreakerDecision:
    """Result of asking the breaker whether SSH should proceed."""

    allowed: bool
    state: State
    # Populated only when allowed is False. Sanitized for the response and
    # already an entry in the plan's reason enum.
    reason: str | None = None
    # Seconds until the breaker would re-permit a probe. None when allowed.
    retry_after_seconds: float | None = None
    # Opaque token identifying a half-open probe admission. Non-None only when
    # ``allowed`` is True and ``state`` is "half_open". Callers must pass it
    # back to ``record_success`` / ``record_failure`` so a stale completion
    # from an abandoned probe cannot resolve a newer probe or close a
    # freshly re-opened circuit.
    probe_token: str | None = None


@dataclass
class _TargetState:
    state: State = "closed"
    failures: deque[float] = None  # type: ignore[assignment]
    opened_at: float | None = None
    probe_in_flight: bool = False
    probe_started_at: float | None = None
    # Identity of the currently in-flight half-open probe. None when no probe
    # is in flight (closed state, open state, or just-expired probe).
    probe_token: str | None = None

    def __post_init__(self) -> None:
        if self.failures is None:
            self.failures = deque()


class RemoteCircuitBreaker:
    """Thread-safe per-target circuit breaker.

    A single instance is shared across all invoke handlers in one process.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        failure_window_seconds: float = 60.0,
        open_duration_seconds: float = 300.0,
        probe_lease_seconds: float = 120.0,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if failure_window_seconds <= 0:
            raise ValueError("failure_window_seconds must be > 0")
        if open_duration_seconds <= 0:
            raise ValueError("open_duration_seconds must be > 0")
        if probe_lease_seconds <= 0:
            raise ValueError("probe_lease_seconds must be > 0")
        self._threshold = failure_threshold
        self._window = failure_window_seconds
        self._cooldown = open_duration_seconds
        # Maximum time we expect a probe to take before we conclude the worker
        # crashed and the breaker is wedged. Should exceed the agent's
        # connection + diagnostic phase budgets combined with some headroom.
        # Default 120 s gives 30 s of headroom over the plan's default
        # executionTimeoutSeconds=90.
        self._probe_lease = probe_lease_seconds
        self._lock = threading.Lock()
        self._targets: dict[str, _TargetState] = {}
        # Hook so tests can freeze time. Production uses time.monotonic.
        self._now = time.monotonic

    # ── observation API ──────────────────────────────────────────────────────

    def allow(self, target_id: str) -> BreakerDecision:
        """Return whether a remote attempt may proceed for ``target_id``.

        When the breaker is open and within the cooldown, returns allowed=False
        with ``reason='circuit_open'``. After the cooldown one half-open probe
        is allowed; subsequent calls during the probe also return allowed=False
        until the probe resolves via ``record_success`` or ``record_failure``.

        If a half-open probe has been in flight longer than
        ``probe_lease_seconds``, it is treated as abandoned (worker crashed
        between admission and resolution). The breaker re-opens with a fresh
        cooldown so subsequent callers see ``circuit_open`` instead of being
        wedged behind a probe that will never complete.
        """
        now = self._now()
        with self._lock:
            st = self._targets.setdefault(target_id, _TargetState())
            self._prune_locked(st, now)
            self._expire_abandoned_probe_locked(st, now)
            if st.state == "closed":
                return BreakerDecision(allowed=True, state="closed")
            if st.state == "open":
                if st.opened_at is None or (now - st.opened_at) < self._cooldown:
                    remaining = (
                        self._cooldown - (now - st.opened_at)
                        if st.opened_at is not None
                        else self._cooldown
                    )
                    return BreakerDecision(
                        allowed=False,
                        state="open",
                        reason="circuit_open",
                        retry_after_seconds=max(0.0, remaining),
                    )
                # Cooldown elapsed → enter half-open and allow exactly one probe
                st.state = "half_open"
                st.probe_in_flight = True
                st.probe_started_at = now
                st.probe_token = secrets.token_hex(16)
                return BreakerDecision(
                    allowed=True, state="half_open", probe_token=st.probe_token
                )
            # half_open: only the in-flight probe is allowed
            if st.probe_in_flight:
                return BreakerDecision(
                    allowed=False,
                    state="half_open",
                    reason="circuit_open",
                    retry_after_seconds=0.0,
                )
            # No probe in flight in half_open → permit one
            st.probe_in_flight = True
            st.probe_started_at = now
            st.probe_token = secrets.token_hex(16)
            return BreakerDecision(
                allowed=True, state="half_open", probe_token=st.probe_token
            )

    def record_failure(
        self,
        target_id: str,
        reason: FailureReason,
        *,
        probe_token: str | None = None,
    ) -> None:
        """Record a connection-phase failure. May transition the breaker open.

        ``reason`` is validated at runtime against ``BREAKER_FAILURE_REASONS``
        (a static Literal alone is silently bypassable). Passing an unknown
        category — or one of the upstream rejection categories like
        ``circuit_open`` — raises ``ValueError`` so caller bugs surface early.

        ``probe_token`` MUST be the token returned by ``allow()`` if the
        caller was admitted as a half-open probe. When the breaker is in
        half_open with a probe in flight, a stale completion whose token
        doesn't match the current probe is silently ignored — that worker
        was abandoned and a replacement (or none) has taken its place.
        Calls without a token are likewise ignored when a probe is in flight,
        so a closed-state worker's late completion cannot resolve a newer
        probe.

        Pass ``reason`` for two purposes: (1) it eventually surfaces as the
        agent_remote_connection_attempts_total{reason=} metric label;
        (2) future policy might weight certain failures differently.
        """
        if reason not in BREAKER_FAILURE_REASONS:
            raise ValueError(
                f"reason {reason!r} is not a breaker failure category; "
                f"valid values: {sorted(BREAKER_FAILURE_REASONS)!r}"
            )
        now = self._now()
        with self._lock:
            st = self._targets.setdefault(target_id, _TargetState())
            self._prune_locked(st, now)
            self._expire_abandoned_probe_locked(st, now)
            if st.state == "half_open" and st.probe_in_flight:
                if probe_token != st.probe_token:
                    # Stale completion from an abandoned probe (or a closed-
                    # state worker that doesn't own this probe). Ignore.
                    return
                # Probe failed → re-open with a fresh cooldown
                st.state = "open"
                st.opened_at = now
                st.probe_in_flight = False
                st.probe_started_at = None
                st.probe_token = None
                # Keep the failure history for diagnostics but do not duplicate
                # this one — the half-open probe was a single attempt.
                return
            if probe_token is not None:
                # Caller presented a token but the breaker is no longer in
                # half_open with a probe in flight (the probe was abandoned,
                # or another path resolved it). This is a stale completion;
                # do not bump the failure window or reset the cooldown.
                return
            st.failures.append(now)
            self._prune_locked(st, now)
            if len(st.failures) >= self._threshold and st.state != "open":
                st.state = "open"
                st.opened_at = now

    def record_success(
        self,
        target_id: str,
        *,
        probe_token: str | None = None,
    ) -> None:
        """Record a verified successful connection. Closes a half-open breaker
        and clears the failure history.

        ``probe_token`` MUST match the token returned by ``allow()`` if the
        caller was admitted as a half-open probe. The acceptance rules are:

        - In ``half_open`` with a probe in flight: only the matching token
          may close the breaker. Other completions (stale tokens, no token)
          are ignored.
        - In ``open``: a token-bearing completion is stale by definition
          (the probe was abandoned and the breaker re-opened). Ignore it so
          the cooldown is not silently bypassed.
        - In ``closed``: backward-compatible. Closed-state callers without
          a token may clear the failure history on a successful connection.
        """
        now = self._now()
        with self._lock:
            st = self._targets.setdefault(target_id, _TargetState())
            self._prune_locked(st, now)
            self._expire_abandoned_probe_locked(st, now)
            if st.state == "half_open" and st.probe_in_flight:
                if probe_token != st.probe_token:
                    return  # stale completion, ignore
                # Probe succeeded → close the breaker.
                st.state = "closed"
                st.opened_at = None
                st.probe_in_flight = False
                st.probe_started_at = None
                st.probe_token = None
                st.failures.clear()
                return
            if probe_token is not None:
                # Token present but breaker no longer has that probe in
                # flight. Stale; do not transition state.
                return
            if st.state == "open":
                # A closed-state worker (no token) returning successful while
                # the breaker is open due to other failures must not bypass
                # the cooldown.
                return
            # Closed state, no token: backward-compatible — clear history.
            st.opened_at = None
            st.probe_in_flight = False
            st.probe_started_at = None
            st.probe_token = None
            st.failures.clear()

    # ── introspection (for metrics/audit, not for control flow) ──────────────

    def snapshot(self, target_id: str) -> tuple[State, int]:
        """Return current state and current-window failure count."""
        now = self._now()
        with self._lock:
            st = self._targets.get(target_id)
            if st is None:
                return "closed", 0
            self._prune_locked(st, now)
            return st.state, len(st.failures)

    def states(self) -> dict[str, State]:
        """Return a snapshot of all known targets and their current states.

        Useful for emitting the agent_remote_circuit_state{target_id} gauge.
        """
        with self._lock:
            return {tid: st.state for tid, st in self._targets.items()}

    # ── internals ────────────────────────────────────────────────────────────

    def _prune_locked(self, st: _TargetState, now: float) -> None:
        cutoff = now - self._window
        while st.failures and st.failures[0] < cutoff:
            st.failures.popleft()

    def _expire_abandoned_probe_locked(self, st: _TargetState, now: float) -> None:
        """If a half-open probe has been in flight past its lease, treat it
        as a failed probe (worker crashed). Re-open with a fresh cooldown so
        subsequent callers do not wedge waiting for record_success/failure
        that will never come. Clearing ``probe_token`` ensures any late
        completion from the abandoned worker will fail the token check and
        be silently ignored."""
        if (
            st.state == "half_open"
            and st.probe_in_flight
            and st.probe_started_at is not None
            and (now - st.probe_started_at) >= self._probe_lease
        ):
            st.state = "open"
            st.opened_at = now
            st.probe_in_flight = False
            st.probe_started_at = None
            st.probe_token = None
