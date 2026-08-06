"""Silences — stop investigating a condition somebody already understands.

These endpoints sit behind interactive user auth, never the webhook token. The
webhook token authenticates a machine that is allowed to *report* alerts; if it
leaks, whoever holds it must not also be able to switch investigation off for
production.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import alert_silences
import auth
import db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/alerts/silences", tags=["alerts"])

# A silence is a promise to stop looking. An unbounded one is how a cluster
# goes unwatched for a month because somebody silenced an alert in April, so
# the TTL is required and capped rather than defaulted to forever.
MAX_TTL_SECONDS = 7 * 24 * 60 * 60


class Matcher(BaseModel):
    label: str
    op: str = "="
    value: str = ""


class SilenceCreate(BaseModel):
    matchers: list[Matcher]
    # Required, and with no default: "why is this silenced" is the question
    # asked when an alert turns out to have mattered, and a default would be
    # answered with whatever the default said.
    reason: str = Field(min_length=1)
    ttl_seconds: int = Field(gt=0, le=MAX_TTL_SECONDS)


def _actor(request: Request) -> str:
    """Who created this silence.

    With auth disabled — desktop mode and local dev — there is no user to
    attribute it to, and refusing would make the feature unusable in the mode
    most people try first.
    """
    user = auth.require_current_user(request)
    return str(user.get("email") or user.get("id") or "local")


@router.post("", status_code=201)
def create_silence(request: Request, body: SilenceCreate) -> dict:
    try:
        matchers = alert_silences.validate_matchers(
            [m.model_dump() for m in body.matchers]
        )
    except alert_silences.InvalidMatcher as exc:
        # 400, not 500: every one of these is something the caller can fix, and
        # the message says what.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    created_by = _actor(request)
    silence = db.create_silence(
        silence_id=str(uuid.uuid4()),
        matchers=matchers,
        reason=body.reason,
        created_by=created_by,
        ttl_seconds=body.ttl_seconds,
    )
    logger.info(
        "silence %s created by %s for %ss: %s",
        silence["id"],
        created_by,
        body.ttl_seconds,
        body.reason,
    )
    return silence


@router.get("")
def list_silences(request: Request, include_inactive: bool = False) -> dict:
    auth.require_current_user(request)
    silences = (
        db.list_all_silences() if include_inactive else db.list_active_silences()
    )
    return {"silences": silences, "count": len(silences)}


@router.get("/{silence_id}")
def get_silence(request: Request, silence_id: str) -> dict:
    auth.require_current_user(request)
    silence = db.get_silence(silence_id)
    if not silence:
        raise HTTPException(status_code=404, detail="Silence not found")
    return silence


@router.delete("/{silence_id}")
def revoke_silence(request: Request, silence_id: str) -> dict:
    actor = _actor(request)
    if not db.get_silence(silence_id):
        raise HTTPException(status_code=404, detail="Silence not found")

    # False means it was already expired or revoked. Not an error — the caller
    # wanted it not in force, and it is not in force.
    revoked = db.revoke_silence(silence_id)
    if revoked:
        logger.info("silence %s revoked by %s", silence_id, actor)
    return {"id": silence_id, "revoked": revoked}
