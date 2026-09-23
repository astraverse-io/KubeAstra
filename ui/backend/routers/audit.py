"""Audit trail — read endpoints.

Read-only by design. Nothing here can write, edit or delete a row: the value
of the record is that the application cannot rewrite it, and an endpoint that
could would undo that whether or not anyone used it.

Everything sits behind interactive user auth. The audit trail says which
clusters exist, who touched them and when, and that is exactly the shape of
information worth withholding from an unauthenticated caller.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

import audit
import auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])

# Verification recomputes a hash per row over the whole table. On a busy
# instance that is not a request anyone should be able to trigger in a loop,
# so it is bounded by default and the caller opts into more.
_DEFAULT_VERIFY_LIMIT = 5000


@router.get("/events")
def list_events(
    request: Request,
    session_id: Optional[str] = None,
    actor_id: Optional[str] = None,
    cluster: Optional[str] = None,
    event_type: Optional[str] = None,
    severity: Optional[str] = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Filtered, newest first. Filters are ANDed."""
    auth.require_current_user(request)

    events = audit.query(
        session_id=session_id,
        actor_id=actor_id,
        cluster=cluster,
        event_type=event_type,
        severity=severity,
        limit=limit,
        offset=offset,
    )
    return {"events": events, "count": len(events), "limit": limit, "offset": offset}


@router.get("/replay/{session_id}")
def replay_session(request: Request, session_id: str) -> dict:
    """Every event for one session, oldest first — the order it happened in.

    Not paginated. A session's history is bounded by the session, and a
    timeline with a page break in the middle is not a timeline.
    """
    auth.require_current_user(request)

    events = audit.replay(session_id)
    if not events:
        # 200 with an empty list, not 404: "this session did nothing auditable"
        # and "this session does not exist" are different answers, and the
        # audit trail is not the authority on which one applies.
        return {"session_id": session_id, "events": [], "count": 0}
    return {"session_id": session_id, "events": events, "count": len(events)}


@router.get("/verify")
def verify(
    request: Request,
    limit: int = Query(default=_DEFAULT_VERIFY_LIMIT, ge=1, le=100_000),
) -> dict:
    """Recompute the hash chain and report the first break.

    A false result is not proof of malice — pruning old rows breaks the chain
    at the seam by design, and the response says so rather than leaving the
    reader to conclude the worst.
    """
    auth.require_current_user(request)

    result = audit.verify_chain(limit=limit)
    if not result["ok"]:
        result["note"] = (
            "A break can also be the retention prune: deleting old rows leaves "
            "the oldest survivor pointing at a predecessor that no longer "
            "exists. Check whether the break is at the start of the range "
            "before treating it as tampering."
        )
    return result


@router.get("/export")
def export_jsonl(
    request: Request,
    session_id: Optional[str] = None,
    cluster: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = Query(default=1000, ge=1, le=10_000),
) -> StreamingResponse:
    """JSONL, for SIEM ingestion.

    Streamed rather than assembled: an export is the one read here with no
    natural bound, and building the whole body in memory first is how a large
    table becomes an outage.
    """
    auth.require_current_user(request)

    events = audit.query(
        session_id=session_id, cluster=cluster, event_type=event_type, limit=limit
    )

    def lines():
        for event in events:
            yield json.dumps(event, sort_keys=True, default=str) + "\n"

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="kubeastra-audit.jsonl"'},
    )


@router.get("/event-types")
def event_types(request: Request) -> dict:
    """The catalogue, so a filter UI does not have to hardcode it."""
    auth.require_current_user(request)

    return {
        "event_types": sorted(
            value
            for name, value in vars(audit.EventType).items()
            if not name.startswith("_") and isinstance(value, str)
        )
    }
