"""Phase 1.3 — thumbs-up / thumbs-down feedback endpoint.

Frontend POSTs ``{capture_id, rating, reason?}``. We promote (rating=up)
or quarantine (rating=down) the matching entry in the RAG store. Errors
are returned to the caller — unlike the capture write path, feedback
intent is explicit so callers should see if their action failed.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

import db
import auth

logger = logging.getLogger(__name__)
router = APIRouter()

_VALID_FEEDBACK_RATINGS = {"up", "down"}
_VALID_FEEDBACK_OUTCOMES = {"accepted", "failed", "rejected"}


class FeedbackRequest(BaseModel):
    capture_id: str = Field(
        description="The session_memory point id returned in a prior chat response."
    )
    rating: str = Field(
        description="'up' to promote to runbook; 'down' to quarantine."
    )
    reason: Optional[str] = Field(
        default=None,
        description="Optional free-text reason. Redacted and stored in feedback audit records.",
    )
    session_id: Optional[str] = Field(default=None)
    prompt: Optional[str] = Field(
        default=None,
        description="User prompt snapshot for feedback review. Redacted before storage.",
    )
    response: Optional[str] = Field(
        default=None,
        description="Assistant answer snapshot for feedback review. Redacted before storage.",
    )
    tool_used: Optional[str] = Field(default=None)


class FeedbackResponse(BaseModel):
    ok: bool
    detail: dict


class FeedbackEventsResponse(BaseModel):
    events: list[dict]


def _redact_feedback_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    try:
        from services.rag.redaction import redact
        return redact(value)
    except Exception:
        return value


def _prepare_feedback_snapshot(value: Optional[str], limit: int = 8000) -> Optional[str]:
    if not value:
        return value
    redacted = _redact_feedback_text(value)
    if redacted and len(redacted) > limit:
        return redacted[:limit] + "\n...[truncated]"
    return redacted


def _redact_feedback_result(result: Optional[dict]) -> Optional[dict]:
    if result is None:
        return None
    try:
        redacted = _redact_feedback_text(json.dumps(result, default=str))
        return json.loads(redacted or "{}")
    except Exception:
        return result


def _audit_feedback(
    *,
    capture_id: str,
    rating: str,
    outcome: str,
    session_id: Optional[str],
    reason: Optional[str],
    prompt: Optional[str] = None,
    response: Optional[str] = None,
    tool_used: Optional[str] = None,
    action_result: Optional[dict] = None,
    error: Optional[str] = None,
) -> Optional[int]:
    try:
        return db.save_feedback_event(
            capture_id=capture_id,
            rating=rating,
            outcome=outcome,
            session_id=session_id,
            reason=_redact_feedback_text(reason),
            prompt_text=_prepare_feedback_snapshot(prompt),
            response_text=_prepare_feedback_snapshot(response),
            tool_used=_prepare_feedback_snapshot(tool_used, limit=200),
            action_result=_redact_feedback_result(action_result),
            error=error,
        )
    except Exception as exc:
        logger.warning("feedback audit DB save failed: %s", exc)
        return None


@router.post("/feedback", response_model=FeedbackResponse)
def submit_feedback(req: FeedbackRequest, request: Request = None):
    """Promote or quarantine a captured session entry."""
    if auth.auth_enabled():
        if not req.session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        if request is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        auth.require_owned_session(request, req.session_id)

    rating = (req.rating or "").strip().lower()
    if rating not in ("up", "down"):
        audit_id = _audit_feedback(
            capture_id=req.capture_id,
            rating=rating,
            outcome="rejected",
            session_id=req.session_id,
            reason=req.reason,
            prompt=req.prompt,
            response=req.response,
            tool_used=req.tool_used,
            error="rating must be 'up' or 'down'",
        )
        logger.info(
            "feedback %s",
            json.dumps({
                "event": "feedback",
                "outcome": "rejected",
                "reason": "invalid_rating",
                "capture_id": req.capture_id,
                "rating": rating,
                "session": req.session_id or "-",
                "feedback_event_id": audit_id,
            }),
        )
        return FeedbackResponse(
            ok=False,
            detail={
                "error": "rating must be 'up' or 'down'",
                "feedback_event_id": audit_id,
            },
        )

    if req.capture_id.startswith("message:"):
        result = {
            "ok": True,
            "audit_only": True,
            "message": "Feedback recorded without RAG promotion because this answer was not captured.",
        }
        audit_id = _audit_feedback(
            capture_id=req.capture_id,
            rating=rating,
            outcome="accepted",
            session_id=req.session_id,
            reason=req.reason,
            prompt=req.prompt,
            response=req.response,
            tool_used=req.tool_used,
            action_result=result,
        )
        result["feedback_event_id"] = audit_id
        logger.info(
            "feedback %s",
            json.dumps({
                "event": "feedback",
                "outcome": "accepted",
                "mode": "audit_only",
                "capture_id": req.capture_id,
                "rating": rating,
                "session": req.session_id or "-",
                "feedback_event_id": audit_id,
            }, default=str),
        )
        return FeedbackResponse(ok=True, detail=result)

    from services.rag.promotion import promote, quarantine
    if rating == "up":
        result = promote(req.capture_id, by_user=req.session_id)
    else:
        result = quarantine(req.capture_id, reason=req.reason, by_user=req.session_id)

    outcome = "accepted" if result.get("ok") else "failed"
    audit_id = _audit_feedback(
        capture_id=req.capture_id,
        rating=rating,
        outcome=outcome,
        session_id=req.session_id,
        reason=req.reason,
        prompt=req.prompt,
        response=req.response,
        tool_used=req.tool_used,
        action_result=result,
        error=result.get("error"),
    )
    result = dict(result)
    result["feedback_event_id"] = audit_id

    logger.info(
        "feedback %s",
        json.dumps({
            "event": "feedback",
            "outcome": outcome,
            "capture_id": req.capture_id,
            "rating": rating,
            "session": req.session_id or "-",
            "feedback_event_id": audit_id,
            "result": {
                "ok": bool(result.get("ok")),
                "error": result.get("error"),
                "already_promoted": bool(result.get("already_promoted")),
                "deleted": result.get("deleted"),
            },
        }, default=str),
    )
    return FeedbackResponse(ok=bool(result.get("ok")), detail=result)


@router.get("/feedback/events", response_model=FeedbackEventsResponse)
def list_feedback_events(
    request: Request = None,
    session_id: Annotated[Optional[str], Query()] = None,
    capture_id: Annotated[Optional[str], Query()] = None,
    rating: Annotated[Optional[str], Query(description="'up' or 'down'")] = None,
    outcome: Annotated[Optional[str], Query(description="'accepted', 'failed', or 'rejected'")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    """Return persisted feedback audit events for beta triage."""
    rating = (rating or "").strip().lower() or None
    outcome = (outcome or "").strip().lower() or None
    if rating and rating not in _VALID_FEEDBACK_RATINGS:
        raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'")
    if outcome and outcome not in _VALID_FEEDBACK_OUTCOMES:
        raise HTTPException(status_code=400, detail="outcome must be 'accepted', 'failed', or 'rejected'")
    user_id = None
    if auth.auth_enabled():
        if request is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        user = auth.require_current_user(request)
        if session_id:
            auth.require_owned_session(request, session_id)
        elif user.get("role") != "admin":
            user_id = user["id"]
    return FeedbackEventsResponse(
        events=db.get_feedback_events(
            session_id=session_id,
            user_id=user_id,
            capture_id=capture_id,
            rating=rating,
            outcome=outcome,
            limit=limit,
        )
    )
