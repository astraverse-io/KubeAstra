"""Desktop local-folder grant endpoints (Phase 1).

Manage the folder-access grants the desktop agent reads through. Registered ONLY
in desktop mode (main.py, inside `if DESKTOP_MODE:`), so these routes 404 in
server mode. They sit behind the existing `desktop_security` localhost + token +
origin boundary like every other desktop route.

The native folder picker runs in the UI (tauri-plugin-dialog); the picked path is
POSTed here. The server re-resolves and re-validates it — it never trusts the
client path blindly (a sensitive root like ~/.ssh is refused even if picked).

Grant/revoke are audited here (the human action point); the boundary's grant CRUD
stays side-effect-free so it can be unit-tested without a database.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import audit
import desktop_folders as folders

router = APIRouter(prefix="/desktop/folders", tags=["Desktop"])


class GrantRequest(BaseModel):
    root: str = Field(..., description="Absolute path the user picked in the native dialog.")
    mode: str = Field("read", description='"read" or "write" (write unused in Phase 1).')


@router.get("/grants")
def list_grants() -> dict:
    """All current grants (id, root, mode, granted_at, last_used). No secrets."""
    return {"grants": folders.list_grants()}


@router.post("/grant")
def create_grant(body: GrantRequest) -> dict:
    if body.mode not in ("read", "write"):
        raise HTTPException(status_code=400, detail="mode must be 'read' or 'write'")
    try:
        resolved = Path(body.root).expanduser().resolve()
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="invalid root path")
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="root is not an existing directory")
    if folders.is_forbidden_root(resolved):
        raise HTTPException(status_code=400, detail="refusing to grant a sensitive system folder")

    grant = folders.add_grant(str(resolved), body.mode)
    audit.emit(
        audit.EventType.FOLDER_GRANT,
        actor_type="user",
        subject=grant["root"],
        payload={"root": grant["root"], "mode": grant["mode"]},
    )
    return grant


@router.delete("/grant/{grant_id}")
def revoke_grant(grant_id: str) -> dict:
    revoked = folders.revoke_grant(grant_id)
    if revoked:
        audit.emit(
            audit.EventType.FOLDER_REVOKE,
            actor_type="user",
            subject=grant_id,
            payload={"id": grant_id},
        )
    return {"revoked": revoked}
