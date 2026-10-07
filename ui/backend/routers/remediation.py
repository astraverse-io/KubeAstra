"""Proposing a fix, and approving one.

A proposal changes nothing. Only an approval, given by a person and still
unexpired, makes one actionable — and even then this router does not act. It
records that the action is authorised; execution stays in `services/plans.py`
behind the confirmation-token machinery it already has.

Keeping those apart is the point. A policy you can bypass by calling a
different function in the same module is not a policy.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import alert_remediation
import audit
import auth
import remediation_executor
import db
import log_safety

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/remediation", tags=["remediation"])


def _settings():
    from config.settings import get_settings

    return get_settings()


def _policy_for(cluster_id: str = "") -> alert_remediation.Policy:
    settings = _settings()
    return alert_remediation.resolve_policy(
        enabled=settings.alert_auto_remediation_enabled,
        global_actions=settings.alert_auto_remediation_allowed_actions,
    )


class ProposalCreate(BaseModel):
    investigation_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    arguments: dict = Field(default_factory=dict)
    # Required. The person approving needs to know why, and "the model
    # suggested it" is not why.
    rationale: str = Field(min_length=1)
    cluster_id: str = ""


class Decision(BaseModel):
    approve: bool
    note: str = ""


class PlanApproval(BaseModel):
    # The non-high-risk step ids this one admin decision authorizes. High-risk
    # steps are excluded and approved individually via /proposals/{id}/decision.
    step_ids: list[str] = Field(default_factory=list)
    note: str = ""


@router.get("/policy")
def get_policy(request: Request, cluster_id: str = "") -> dict:
    """What this deployment currently permits, and why.

    Readable before anything is proposed, so "can it restart a deployment" is
    answerable without triggering an alert to find out.
    """
    auth.require_current_user(request)
    policy = _policy_for(cluster_id)
    return {
        "enabled": _settings().alert_auto_remediation_enabled,
        "allowed_actions": sorted(policy.allowed),
        "proposable_actions": sorted(alert_remediation.PROPOSABLE_ACTIONS),
        "reasons": list(policy.reasons),
    }


@router.post("/proposals", status_code=201)
def create_proposal(request: Request, body: ProposalCreate) -> dict:
    auth.require_current_user(request)

    try:
        alert_remediation.check(body.action, _policy_for(body.cluster_id))
    except alert_remediation.RemediationNotPermitted as exc:
        # 403 rather than 400: the request is well-formed, the deployment just
        # does not allow it. The message carries which layer said no.
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    proposal = db.create_remediation_proposal(
        proposal_id=str(uuid.uuid4()),
        investigation_id=body.investigation_id,
        action=body.action,
        arguments=body.arguments,
        rationale=body.rationale,
        ttl_seconds=_settings().alert_remediation_approval_ttl_seconds,
        cluster_id=body.cluster_id,
    )
    logger.info(
        "remediation proposed for %s: %s",
        log_safety.one_line(body.investigation_id),
        log_safety.one_line(body.action),
    )
    return proposal


@router.get("/proposals")
def list_proposals(
    request: Request, investigation_id: str = "", pending_only: bool = False
) -> dict:
    auth.require_current_user(request)
    proposals = db.list_remediation_proposals(
        investigation_id=investigation_id, pending_only=pending_only
    )
    return {"proposals": proposals, "count": len(proposals)}


@router.post("/proposals/{proposal_id}/decision")
def decide(request: Request, proposal_id: str, body: Decision) -> dict:
    """Approve or reject. Admin-gated: approving authorises a change to a
    cluster, which is not the same permission as reading an investigation."""
    user = auth.require_current_user(request)
    if user and not auth.is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    decided_by = str(user.get("email") or user.get("id") or "local")

    existing = db.get_remediation_proposal(proposal_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Proposal not found")

    decided = db.decide_remediation_proposal(
        proposal_id, body.approve, decided_by, body.note
    )
    if decided is None:
        # Already decided, or expired. Saying which avoids an operator
        # retrying a decision that can never take.
        raise HTTPException(
            status_code=409,
            detail=f"proposal is {existing['status']}, not pending",
        )

    logger.info(
        "remediation %s %s by %s",
        log_safety.one_line(proposal_id),
        "approved" if body.approve else "rejected",
        log_safety.one_line(decided_by),
    )
    # The person who authorised a change to a cluster. A log line is not a
    # record — it rotates, and nothing detects an edit to it.
    audit.emit(
        audit.EventType.APPROVAL_GRANTED if body.approve
        else audit.EventType.APPROVAL_DENIED,
        actor_type="user",
        actor_id=decided_by,
        cluster=existing.get("cluster_id") or "default",
        subject=f"{existing.get('action')} {existing.get('arguments')}",
        severity="warn" if body.approve else "info",
        payload={"proposal_id": proposal_id, "action": existing.get("action"),
                 "arguments": existing.get("arguments"), "note": body.note},
    )
    return decided


@router.post("/proposals/{proposal_id}/execute")
def execute(request: Request, proposal_id: str) -> dict:
    """Carry out an approved proposal.

    Admin-gated, and separate from approval on purpose: approving is a
    judgement, running it is an act, and requiring both makes "approve and run"
    two decisions rather than one click that does both.

    The work is in remediation_executor, which re-checks policy, namespace,
    arguments and the rate cap before touching anything — nothing is trusted
    from the approval, which only records that a human agreed at some point.
    """
    user = auth.require_current_user(request)
    if user and not auth.is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        return remediation_executor.execute_proposal(proposal_id)
    except alert_remediation.RemediationNotPermitted as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except remediation_executor.RemediationFailed as exc:
        # 502: the request was allowed and the cluster refused or failed. An
        # operator needs to tell that apart from "policy said no".
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _plan_steps(run: dict) -> list[dict]:
    """The step list from a stored Pilot run's plan, or []."""
    plan = (run or {}).get("plan") or {}
    inner = plan.get("plan") if isinstance(plan.get("plan"), dict) else plan
    return (inner or {}).get("steps") or []


@router.post("/plans/{run_id}/approve")
def approve_plan(request: Request, run_id: str, body: PlanApproval) -> dict:
    """Option B: one admin decision authorizes a Pilot plan's NON-high-risk steps.

    Admin-gated, like a proposal decision — approving authorises changes to a
    cluster. The operator sees the concrete steps before approving; high-risk
    steps are refused here and must be approved one by one through
    /proposals/{id}/decision, so the spine's "a named person authorised this
    change" invariant holds without N prompts.
    """
    user = auth.require_current_user(request)
    if user and not auth.is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    approved_by = str((user or {}).get("email") or (user or {}).get("id") or "local")

    run = db.get_pilot_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="pilot run not found")

    steps = _plan_steps(run)
    by_id = {s.get("id"): s for s in steps}
    if not body.step_ids:
        raise HTTPException(status_code=422, detail="step_ids is required")

    unknown = [sid for sid in body.step_ids if sid not in by_id]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown step ids: {unknown}")
    high_risk = [sid for sid in body.step_ids if by_id[sid].get("risk") == "high"]
    if high_risk:
        raise HTTPException(
            status_code=422,
            detail=(
                f"high-risk steps cannot be plan-approved in bulk: {high_risk}. "
                f"Approve each individually via /proposals/{{id}}/decision."
            ),
        )

    approval = db.create_plan_approval(run_id, approved_by, body.step_ids)
    logger.info(
        "pilot plan %s approved by %s (%d steps)",
        log_safety.one_line(run_id), log_safety.one_line(approved_by), len(body.step_ids),
    )
    audit.emit(
        audit.EventType.PLAN_APPROVAL_GRANTED,
        actor_type="user",
        actor_id=approved_by,
        cluster=run.get("cluster_id") or "default",
        subject=f"pilot plan {run.get('pilot', '')} -> {run.get('target', '')}",
        severity="warn",
        payload={"run_id": run_id, "step_ids": body.step_ids, "note": body.note},
    )
    return approval
