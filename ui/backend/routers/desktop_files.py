"""Desktop pending-write endpoints: review, apply, discard (Phase 2, PR 4).

``propose_file_edit`` never writes; it parks a validated pending write. These
endpoints are the human's side of that: the approval card fetches the **real**
diff (the model only ever saw a diff of the sanitized views), and **apply** is
the only code path in the product that writes a user's file.

Apply trusts nothing from propose time. It consumes the token (single-use even
if the write is then refused) and re-runs the whole boundary: the write grant
must still be held, the target re-resolves to the same path, is not a symlink,
not deny-listed, and the file still hashes to what the user reviewed. Only then
the atomic write. Refusals are audited as ``folder.write_denied``; a success as
``folder.write`` with {root, rel_path, byte counts, validated} — never content.

Registered only in desktop mode (main.py, inside ``if DESKTOP_MODE:``), behind
the ``desktop_security`` localhost + token + origin boundary like every other
desktop route. See DESKTOP_AGENT_PHASE2_SPEC.md §2 (PR 4).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import audit
import desktop_folders as df
import desktop_writes as dw

router = APIRouter(prefix="/desktop/files", tags=["Desktop"])

# WriteRefused reasons that mean "the file moved under us" → 409, not 400.
_CONFLICT_REASONS = frozenset({"file_changed", "file_exists", "path_changed"})


class TokenBody(BaseModel):
    token: str = Field(..., min_length=1, max_length=200)


def _deny(pw: dw.PendingWrite, reason: str, status: int, detail: str) -> HTTPException:
    audit.emit(
        audit.EventType.FOLDER_WRITE_DENIED,
        actor_type="user",
        subject=pw.rel_path,
        severity="warn",
        payload={"root": pw.root, "rel_path": pw.rel_path, "reason": reason},
    )
    return HTTPException(status_code=status, detail=detail)


@router.get("/pending/{token}")
def get_pending(token: str) -> dict:
    """The pending write for the approval card, including the real diff. Does
    not consume the token."""
    pw = dw.pending_write_store.get(token)
    if pw is None:
        raise HTTPException(status_code=404, detail="proposed edit expired or already handled")
    return {
        "token": pw.token,
        "path": pw.rel_path,
        "root": pw.root,
        "created": pw.created,
        "reason": pw.extra.get("reason", ""),
        "diff": pw.diff,
        "validation": pw.validation,
        "expires_at": pw.expires_at,
    }


@router.post("/apply")
def apply_write(body: TokenBody) -> dict:
    pw = dw.pending_write_store.pop(body.token)
    if pw is None:
        raise HTTPException(status_code=404, detail="proposed edit expired or already handled")

    # Check the link itself before the grant lookup: grant_for follows symlinks,
    # so a target swapped for a link pointing elsewhere would otherwise read as
    # "access revoked" rather than what it is.
    if Path(pw.abs_path).is_symlink():
        raise _deny(pw, "symlink_target", 400,
                    "refused: the file was replaced by a symlink; nothing was written")
    grant = df.grant_for(pw.abs_path, "write")
    if grant is None:
        if df._lexically_in_any_grant(pw.abs_path, "write"):
            raise _deny(pw, "outside_grant", 400,
                        "refused: the path now resolves outside the granted folder; nothing was written")
        raise _deny(pw, "no_write_grant", 403,
                    "write access to this folder was revoked; nothing was written")
    try:
        resolved, exists = dw.resolve_write_target(pw.abs_path, grant)
        if str(resolved) != pw.abs_path:
            raise dw.WriteRefused("path_changed")
        if exists and pw.created:
            raise dw.WriteRefused("file_exists")
        if not exists and not pw.created:
            raise dw.WriteRefused("file_changed", "file no longer exists")
        dw.atomic_write(resolved, pw.after, expected_sha=pw.before_sha)
    except df.AccessDenied as ad:
        raise _deny(pw, ad.reason, 400, f"refused: {ad.reason}; nothing was written")
    except dw.WriteRefused as wr:
        if wr.reason in _CONFLICT_REASONS:
            raise _deny(pw, wr.reason, 409,
                        "the file changed on disk since this edit was proposed; nothing was "
                        "written — ask the agent to propose it again")
        raise _deny(pw, wr.reason, 400, f"refused: {wr.reason}; nothing was written")

    audit.emit(
        audit.EventType.FOLDER_WRITE,
        actor_type="user",
        subject=pw.rel_path,
        payload={
            "root": pw.root,
            "rel_path": pw.rel_path,
            "created": pw.created,
            "bytes_before": 0 if pw.before is None else len(pw.before.encode("utf-8")),
            "bytes_after": len(pw.after.encode("utf-8")),
            "validated": bool(pw.validation.get("validated")),
        },
    )
    df.touch_last_used(grant["id"])
    return {"written": True, "path": pw.rel_path, "created": pw.created}


@router.post("/discard")
def discard_write(body: TokenBody) -> dict:
    return {"discarded": dw.pending_write_store.pop(body.token) is not None}
