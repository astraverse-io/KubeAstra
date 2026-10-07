"""Resumable, idempotent runner for an approved Upgrade Pilot plan.

Drives a plan's steps in order with the safety properties
``internal_docs/features/PILOTS_PLAN.md`` §Phase 5 requires, and is pure of I/O:
the caller injects ``verify``, ``execute`` and ``record`` callables (the server
wires them to the real kubectl / helm / #81 actions and ``db.upsert_pilot_step``;
tests pass fakes). So the orchestration — ordering, verify-first idempotency,
approval gating, halt-on-failure, per-cluster locking — is unit-testable without
a cluster.

Guarantees:
- **Idempotent** — every step is ``verify``-ed BEFORE execution; an already
  satisfied step is skipped, so re-running after a halt resumes cleanly.
- **Approval-gated** — a non-high-risk step runs only if its id is in the plan
  approval; a high-risk step only if it has its own individual approval. Neither
  is ever inferred, and an unauthorized step halts the run (ordering is a
  dependency), leaving earlier steps applied.
- **Control-plane / advisory steps are NEVER executed here** (they are copy-only
  commands the operator runs).
- **Halts on the first failed apply or failed post-apply verify.**
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

Step = dict  # a plan step dict: {id, order, kind, risk, advisory, ...}

# Step kinds that are rendered as commands for a human, never auto-executed.
_ADVISORY_KINDS = {"control_plane"}


@dataclass
class StepOutcome:
    step_id: str
    kind: str
    status: str  # advisory | verified | applied | pending | failed
    detail: str = ""


@dataclass
class RunOutcome:
    run_id: str
    status: str  # done | blocked | halted | locked
    steps: list[StepOutcome] = field(default_factory=list)


def run_plan(
    *,
    run_id: str,
    steps: list[Step],
    authorized_step_ids: set[str],
    individual_approved_step_ids: Optional[set[str]] = None,
    verify: Callable[[Step], bool],
    execute: Callable[[Step], bool],
    record: Optional[Callable[..., None]] = None,
    lock_ok: bool = True,
) -> RunOutcome:
    """Execute an approved plan. See the module docstring for the guarantees."""
    authorized = set(authorized_step_ids or ())
    individual = set(individual_approved_step_ids or ())
    outcomes: list[StepOutcome] = []

    def _emit(step: Step, status: str, detail: str = "") -> None:
        oc = StepOutcome(step_id=step.get("id", ""), kind=step.get("kind", ""), status=status, detail=detail)
        outcomes.append(oc)
        if record is not None:
            record(step_id=oc.step_id, ord=step.get("order", 0), kind=oc.kind, status=status)

    if not lock_ok:
        # Another apply run is active on this cluster — do not touch anything.
        return RunOutcome(run_id=run_id, status="locked", steps=outcomes)

    for step in sorted(steps, key=lambda s: s.get("order", 0)):
        sid = step.get("id", "")
        kind = step.get("kind", "")
        risk = step.get("risk", "")

        # Control-plane / advisory: copy-only commands, never executed here.
        if kind in _ADVISORY_KINDS or step.get("advisory"):
            _emit(step, "advisory", "copy-only; run manually")
            continue

        # Idempotency: already satisfied? skip (makes re-runs safe).
        if verify(step):
            _emit(step, "verified", "already satisfied")
            continue

        # Authorization — never inferred.
        is_authorized = (sid in individual) if risk == "high" else (sid in authorized)
        if not is_authorized:
            _emit(step, "pending", "awaiting approval")
            return RunOutcome(run_id=run_id, status="blocked", steps=outcomes)

        # Apply, then re-verify.
        if not execute(step):
            _emit(step, "failed", "apply failed")
            return RunOutcome(run_id=run_id, status="halted", steps=outcomes)
        if not verify(step):
            _emit(step, "failed", "verify failed after apply")
            return RunOutcome(run_id=run_id, status="halted", steps=outcomes)
        _emit(step, "applied")

    return RunOutcome(run_id=run_id, status="done", steps=outcomes)
