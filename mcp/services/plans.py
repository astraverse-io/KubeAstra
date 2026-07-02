"""Multi-step remediation plans (Feature C).

A Plan is an ordered list of destructive Kubernetes operations the assistant
proposes for a single incident. Each step is one of the allow-listed
destructive tools (delete_pod, rollout_restart, scale_deployment, apply_patch)
plus its arguments and a human-readable `why`.

Flow:
  1. propose_remediation_plan(issue, steps) → server validates the steps,
     stores them under a plan_id, returns the plan.
  2. UI renders the plan. For each step the user wants to run:
       a. The UI calls the underlying destructive tool with dry_run=True,
          which returns a per-step confirmation_token (Feature B).
       b. The UI calls execute_plan_step(plan_id, step_index, confirmation_token).
  3. execute_plan_step verifies the plan is still valid, then invokes the
     destructive tool with confirm=True + the token.

The plan store is in-memory + single-pod, same caveats as the token store.
TTLs are deliberately short (default 15 min) so plans don't outlive their
context.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from config.settings import get_settings

logger = logging.getLogger(__name__)


# Only these tools may appear in a plan step. Aligned with Feature B.
ALLOWED_TOOLS = frozenset({
    "delete_pod",
    "rollout_restart",
    "scale_deployment",
    "apply_patch",
})

# Required argument keys per tool. Kept tight: extra args are tolerated (the
# downstream wrapper validates them); missing required args are rejected here
# so a malformed plan never reaches kubectl.
_REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "delete_pod":        ("namespace", "pod_name"),
    "rollout_restart":   ("namespace", "deployment_name"),
    "scale_deployment":  ("namespace", "deployment_name", "replicas"),
    "apply_patch":       ("namespace", "resource_type", "resource_name", "patch"),
}

# Plan TTL — long enough for a human to read + approve, short enough that
# cluster state doesn't drift out from under it.
PLAN_TTL_SECONDS = 900  # 15 minutes


@dataclass
class PlanStep:
    step: int
    tool: str
    args: dict[str, Any]
    why: str = ""
    rollback: Optional[dict[str, Any]] = None
    status: str = "pending"   # pending | running | done | failed | skipped
    result: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "tool": self.tool,
            "args": self.args,
            "why": self.why,
            "rollback": self.rollback,
            "status": self.status,
            "result": self.result,
        }


@dataclass
class Plan:
    plan_id: str
    issue: str
    steps: list[PlanStep]
    created_at: float
    expires_at: float
    user: str = "anonymous"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "issue": self.issue,
            "user": self.user,
            "notes": self.notes,
            "created_at_iso": datetime.fromtimestamp(self.created_at).isoformat(),
            "expires_at_iso": datetime.fromtimestamp(self.expires_at).isoformat(),
            "expires_in_seconds": max(0, int(self.expires_at - time.time())),
            "steps": [s.to_dict() for s in self.steps],
        }


class PlanStore:
    """Thread-safe in-memory plan store."""

    def __init__(self):
        self._plans: dict[str, Plan] = {}
        self._lock = threading.Lock()

    def put(self, plan: Plan) -> None:
        with self._lock:
            self._purge_expired_locked()
            self._plans[plan.plan_id] = plan

    def get(self, plan_id: str) -> Optional[Plan]:
        with self._lock:
            self._purge_expired_locked()
            return self._plans.get(plan_id)

    def update_step(self, plan_id: str, step_index: int, **updates) -> Optional[Plan]:
        with self._lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                return None
            if step_index < 0 or step_index >= len(plan.steps):
                return None
            step = plan.steps[step_index]
            for k, v in updates.items():
                if hasattr(step, k):
                    setattr(step, k, v)
            return plan

    def claim_step(
        self,
        plan_id: str,
        step_index: int,
        allowed_from: tuple[str, ...] = ("pending", "failed"),
    ) -> tuple[Optional[Plan], Optional[str]]:
        """Atomically transition a step's status to 'running'.

        Returns (plan, None) on success; (None, current_status) when the step's
        current status is not in `allowed_from` (or step doesn't exist).
        Prevents the "two concurrent execute_step calls clobber each other"
        race that the previous non-atomic check/set had.
        """
        with self._lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                return None, None
            if step_index < 0 or step_index >= len(plan.steps):
                return None, None
            step = plan.steps[step_index]
            if step.status not in allowed_from:
                return None, step.status
            step.status = "running"
            return plan, None

    def _purge_expired_locked(self) -> None:
        now = time.time()
        for pid in [p for p, pl in self._plans.items() if pl.expires_at < now]:
            self._plans.pop(pid, None)


plan_store = PlanStore()


# ── Construction + validation ────────────────────────────────────────────────

class PlanValidationError(ValueError):
    """Raised when a proposed plan is structurally invalid."""


def build_plan(
    issue: str,
    steps: list[dict[str, Any]],
    user: Optional[str] = None,
    notes: str = "",
) -> Plan:
    """Validate raw step dicts and build a Plan. Does not run anything."""
    if not issue or not issue.strip():
        raise PlanValidationError("issue must be non-empty")
    if not steps:
        raise PlanValidationError("plan must contain at least one step")
    if len(steps) > 25:
        raise PlanValidationError("plan is too long (max 25 steps)")

    validated: list[PlanStep] = []
    for i, raw in enumerate(steps, start=1):
        tool = (raw.get("tool") or "").strip()
        args = raw.get("args") or {}
        if not isinstance(args, dict):
            raise PlanValidationError(f"step {i}: args must be an object")

        if tool not in ALLOWED_TOOLS:
            raise PlanValidationError(
                f"step {i}: tool '{tool}' is not allowed. "
                f"Allowed tools: {sorted(ALLOWED_TOOLS)}"
            )

        required = _REQUIRED_ARGS[tool]
        missing = [k for k in required if k not in args]
        if missing:
            raise PlanValidationError(
                f"step {i} ({tool}): missing required args {missing}"
            )

        validated.append(PlanStep(
            step=i,
            tool=tool,
            args=dict(args),
            why=str(raw.get("why", "")),
            rollback=raw.get("rollback"),
        ))

    now = time.time()
    return Plan(
        plan_id="plan_" + secrets.token_urlsafe(12),
        issue=issue.strip(),
        steps=validated,
        created_at=now,
        expires_at=now + PLAN_TTL_SECONDS,
        user=user or "anonymous",
        notes=notes,
    )


# ── Execution ────────────────────────────────────────────────────────────────

def execute_step(
    plan_id: str,
    step_index: int,
    confirmation_token: str,
) -> dict[str, Any]:
    """Execute one step of a stored plan.

    Caller must already have called the underlying destructive tool with
    dry_run=True to obtain the confirmation_token. We then call the same tool
    with confirm=True + the token, which Feature B's machinery validates.
    """
    plan = plan_store.get(plan_id)
    if plan is None:
        return {"success": False, "error": f"plan {plan_id} not found or expired"}

    if step_index < 0 or step_index >= len(plan.steps):
        return {
            "success": False,
            "error": f"step_index {step_index} out of range (plan has {len(plan.steps)} steps)",
        }

    if not confirmation_token:
        return {
            "success": False,
            "error": "confirmation_token is required. Call the underlying tool with "
                     "dry_run=True first to receive one.",
            "step": plan.steps[step_index].to_dict(),
        }

    # Atomically claim the step (pending|failed → running). If we can't claim,
    # the step is either already done or currently being executed by another
    # caller — surface that to the caller instead of double-executing.
    claimed, current_status = plan_store.claim_step(plan_id, step_index)
    if claimed is None:
        existing = plan.steps[step_index]
        if current_status == "done":
            return {
                "success": False,
                "reason": "already_executed",
                "error": "step has already been executed",
                "step": existing.to_dict(),
            }
        if current_status == "running":
            return {
                "success": False,
                "reason": "in_progress",
                "error": "step is currently being executed by another caller",
                "step": existing.to_dict(),
            }
        return {
            "success": False,
            "reason": "unclaimable",
            "error": f"could not claim step (status={current_status})",
            "step": existing.to_dict(),
        }

    step = claimed.steps[step_index]
    audit_plan_event("execute_step_start", plan_id, step_index, step.tool, user=plan.user)

    try:
        result = _dispatch(step.tool, step.args, confirmation_token)
    except Exception as exc:
        plan_store.update_step(plan_id, step_index, status="failed",
                               result={"error": str(exc)})
        audit_plan_event("execute_step_error", plan_id, step_index, step.tool,
                         user=plan.user, reason=str(exc))
        return {"success": False, "error": f"step raised: {exc}"}

    success = bool(result.get("success"))
    plan_store.update_step(
        plan_id,
        step_index,
        status="done" if success else "failed",
        result=result,
    )
    audit_plan_event(
        "execute_step_done" if success else "execute_step_failed",
        plan_id, step_index, step.tool, user=plan.user,
    )

    return {
        "success": success,
        "plan_id": plan_id,
        "step_index": step_index,
        "step": plan_store.get(plan_id).steps[step_index].to_dict(),
    }


def _dispatch(tool: str, args: dict[str, Any], token: str) -> dict[str, Any]:
    """Invoke a destructive wrapper with confirm=True + the token."""
    if tool == "delete_pod":
        from k8s.wrappers import delete_pod
        return delete_pod(
            namespace=args["namespace"],
            pod_name=args["pod_name"],
            grace_period=args.get("grace_period", 30),
            confirm=True,
            confirmation_token=token,
        )
    if tool == "rollout_restart":
        from k8s.wrappers import rollout_restart
        return rollout_restart(
            namespace=args["namespace"],
            deployment_name=args["deployment_name"],
            confirm=True,
            confirmation_token=token,
        )
    if tool == "scale_deployment":
        from k8s.wrappers import scale_deployment
        return scale_deployment(
            namespace=args["namespace"],
            deployment_name=args["deployment_name"],
            replicas=args["replicas"],
            confirm=True,
            confirmation_token=token,
        )
    if tool == "apply_patch":
        from k8s.wrappers import apply_patch
        return apply_patch(
            namespace=args["namespace"],
            resource_type=args["resource_type"],
            resource_name=args["resource_name"],
            patch=args["patch"],
            patch_type=args.get("patch_type", "strategic"),
            confirm=True,
            confirmation_token=token,
        )
    raise PlanValidationError(f"unsupported tool in plan: {tool}")


# ── Audit ────────────────────────────────────────────────────────────────────

def audit_plan_event(
    event: str,
    plan_id: str,
    step_index: Optional[int] = None,
    tool: Optional[str] = None,
    user: str = "anonymous",
    reason: Optional[str] = None,
) -> None:
    """Append a plan-lifecycle event to the same audit log as kubectl + tokens."""
    settings = get_settings()
    if not settings.enable_audit_log:
        return
    try:
        ts = datetime.now().isoformat()
        parts = [ts, f"PLAN_{event.upper()}", f"plan_id={plan_id}", f"user={user}"]
        if step_index is not None:
            parts.append(f"step={step_index}")
        if tool:
            parts.append(f"tool={tool}")
        if reason:
            parts.append(f"reason={reason}")
        line = " | ".join(parts) + "\n"

        path = Path(settings.audit_log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(line)
    except Exception as exc:
        logger.warning("Failed to write plan audit event: %s", exc)
