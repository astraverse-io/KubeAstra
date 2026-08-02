"""Admin endpoints (Production Readiness Phase 1B).

Includes cost summary endpoint, budget limits/resets in later phases.
"""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Query, Request

import auth
import db

from http_errors import internal_error

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/cost-summary")
def get_cost_summary(
    request: Request,
    user_id: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None),
    group_by: str = Query(default="user"),
):
    """Aggregate spend metrics across users, days, or models. Admin-only."""
    if auth.auth_enabled():
        user = auth.require_current_user(request)
        if not auth.is_admin(user):
            raise HTTPException(status_code=403, detail="Admin permissions required")

    if group_by not in ("user", "day", "model"):
        raise HTTPException(status_code=400, detail="Invalid group_by. Must be 'user', 'day', or 'model'")

    if since:
        try:
            from datetime import datetime
            datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid 'since' format. Must be ISO 8601 format (e.g. YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)",
            )

    try:
        data = db.aggregate_run_costs(group_by=group_by, since=since, user_id=user_id)
        return data
    except Exception as exc:
        logger.exception("Failed to aggregate run costs")
        raise internal_error(exc, context="cost aggregation") from exc
