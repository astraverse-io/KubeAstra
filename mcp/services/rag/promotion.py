"""Promotion / quarantine handlers for captured session entries.

These are the actions a feedback endpoint calls when the user clicks
👍 / 👎 on an assistant message that was previously captured:

  - ``promote(capture_id)`` — copy the entry from ``session_memory`` into
    the high-trust ``runbook`` collection with ``verified: true``, and
    delete the original. After this, the entry qualifies for the
    cached short-circuit path in the retrieval router.

  - ``quarantine(capture_id, reason)`` — delete the entry from
    ``session_memory``. The captured answer was misleading or wrong;
    we don't want it surfacing in future searches.

Both operations are idempotent: re-running with the same id is a safe
no-op once the entry has been promoted/quarantined already.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from services.rag.schema import RUNBOOK, SESSION_MEMORY
from services.vector_db import vector_db

logger = logging.getLogger(__name__)


def promote(capture_id: str, *, by_user: Optional[str] = None) -> dict[str, Any]:
    """Promote a session_memory entry to the runbook collection.

    Returns ``{ok, runbook_id, error?}``. Always returns a dict; never
    raises.
    """
    if not capture_id:
        return {"ok": False, "error": "missing capture_id"}

    try:
        vector_db.connect()
    except Exception as exc:
        return {"ok": False, "error": f"vector DB unavailable: {exc}"}

    point, vector = _fetch(SESSION_MEMORY.name, capture_id)
    if point is None:
        # Maybe it was already promoted — check runbook first.
        rb_point, _ = _fetch(RUNBOOK.name, capture_id)
        if rb_point is not None:
            return {"ok": True, "already_promoted": True, "runbook_id": capture_id}
        return {"ok": False, "error": "capture_id not found"}

    # Ensure runbook collection exists.
    try:
        vector_db.ensure_collection_for(RUNBOOK)
    except Exception as exc:
        return {"ok": False, "error": f"ensure_collection failed: {exc}"}

    # Build the runbook payload. Reuse the same id so future calls are
    # idempotent — the same capture promoted twice stays a single runbook.
    payload = dict(point.payload or {})
    resolution = (payload.get("resolution") or "").strip()
    problem = (payload.get("question") or payload.get("error_signature") or "").strip()
    payload.update({
        "problem": problem or payload.get("title", ""),
        "resolution": resolution,
        "commands": payload.get("commands_run", ""),
        "verified": True,
        "upvotes": int(payload.get("upvotes") or 0) + 1,
        "source": "promoted_from_session",
        "promoted_at": _iso_now(),
        "promoted_by": by_user or "anonymous",
        # Drop session-specific fields that don't apply to a runbook.
        "expires_at": None,
    })

    try:
        vector_db.upsert_point(
            collection=RUNBOOK.name,
            point_id=capture_id,
            payload=payload,
            vector=vector,
        )
    except Exception as exc:
        return {"ok": False, "error": f"runbook upsert failed: {exc}"}

    # Remove from session_memory so it doesn't double-surface.
    _delete_point(SESSION_MEMORY.name, capture_id)

    logger.info("promoted capture=%s -> runbook by=%s", capture_id, by_user or "-")
    return {"ok": True, "runbook_id": capture_id, "title": payload.get("title")}


def quarantine(
    capture_id: str,
    *,
    reason: Optional[str] = None,
    by_user: Optional[str] = None,
) -> dict[str, Any]:
    """Delete a session_memory entry (👎 with optional reason).

    The ``reason`` parameter is currently logged-only; a future iteration
    could keep a small audit table so we can learn from rejected captures.
    """
    if not capture_id:
        return {"ok": False, "error": "missing capture_id"}

    try:
        vector_db.connect()
    except Exception as exc:
        return {"ok": False, "error": f"vector DB unavailable: {exc}"}

    deleted = _delete_point(SESSION_MEMORY.name, capture_id)
    logger.info(
        "quarantined capture=%s by=%s deleted=%s reason=%r",
        capture_id, by_user or "-", deleted, reason or "",
    )
    return {"ok": True, "deleted": deleted}


# ── Internals ────────────────────────────────────────────────────────────────

def _fetch(collection: str, point_id: str):
    """Return ``(point, vector)`` or ``(None, None)`` if missing."""
    try:
        # ``retrieve`` is the qdrant-client method; with_vectors=True
        # because we want to copy the embedding over to runbook.
        client = getattr(vector_db, "_client", None)
        if client is None:
            return None, None
        pts = client.retrieve(
            collection_name=collection,
            ids=[point_id],
            with_payload=True,
            with_vectors=True,
        )
        if not pts:
            return None, None
        p = pts[0]
        vec = getattr(p, "vector", None)
        # qdrant-client may return {"": [...]} for named-vector mode; we
        # only use the default unnamed vector.
        if isinstance(vec, dict):
            vec = next(iter(vec.values()), None)
        return p, vec
    except Exception as exc:
        logger.warning("retrieve(%s, %s) failed: %s", collection, point_id, exc)
        return None, None


def _delete_point(collection: str, point_id: str) -> bool:
    try:
        client = getattr(vector_db, "_client", None)
        if client is None:
            return False
        from qdrant_client.http import models as qmodels
        client.delete(
            collection_name=collection,
            points_selector=qmodels.PointIdsList(points=[point_id]),
        )
        return True
    except Exception as exc:
        logger.warning("delete(%s, %s) failed: %s", collection, point_id, exc)
        return False


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()
