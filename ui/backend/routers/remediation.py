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
import auth
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
    return decided
