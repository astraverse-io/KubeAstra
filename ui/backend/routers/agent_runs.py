"""Agent run trace endpoints (harness Phase 1).

Read-only views of the ``agent_runs`` / ``agent_steps`` tables persisted by
``AgentRunRecorder``. Used by the UI's run-details drawer, the eval runner, and
admin debugging.

Authz:
- Normal users see only runs where ``agent_runs.user_id`` matches.
- Admins see any run for support workflows.
- When auth is disabled the endpoints fall back to "no filtering" so anonymous
  developer setups keep working — the production deployment is expected to run
  with auth on.
"""

from typing import Optional
import os
import hmac
import logging

from fastapi import APIRouter, HTTPException, Query, Request

import auth
import db
import postmortem_writer

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/agent-runs")
def list_agent_runs(
    request: Request,
    session_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    """List runs visible to the caller. Most-recent first."""
    if not auth.auth_enabled():
        return {"runs": db.list_agent_runs(session_id=session_id, limit=limit)}

    user = auth.require_current_user(request)
    if auth.is_admin(user):
        # Admins see all runs (optionally filtered by session_id).
        return {"runs": db.list_agent_runs(session_id=session_id, limit=limit)}

    runs = db.list_agent_runs(user_id=user["id"], session_id=session_id, limit=limit)
    return {"runs": runs}


@router.get("/agent-runs/{run_id}")
def get_agent_run_details(run_id: str, request: Request):
    """Return a single run + its steps, enforcing owner/admin access."""
    run = db.get_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    if auth.auth_enabled():
        user = auth.require_current_user(request)
        is_owner = run.get("user_id") == user["id"]
        if not is_owner and not auth.is_admin(user):
            # Don't leak existence — same 404 as missing run.
            raise HTTPException(status_code=404, detail="Run not found")
        access_mode = "owned" if is_owner else "admin_readonly"
    else:
        access_mode = "owned"

    steps = db.get_agent_steps(run_id)
    return {
        "run": run,
        "steps": steps,
        "access_mode": access_mode,
    }


@router.post("/agent-runs/prune")
def prune_agent_runs_endpoint(
    request: Request,
    days: int = Query(default=30, ge=1),
):
    """Prune agent runs older than the specified number of days."""
    # Check for internal/CronJob authorization via token
    token = request.headers.get("X-Prune-Token")
    expected_token = os.environ.get("PRUNE_TOKEN")

    authorized = False
    if expected_token and token and hmac.compare_digest(token, expected_token):
        authorized = True

    if not authorized:
        # Otherwise, require admin user role if auth is enabled
        if auth.auth_enabled():
            user = auth.require_current_user(request)
            if not auth.is_admin(user):
                raise HTTPException(status_code=403, detail="Admin permissions required")
        else:
            logger.warning("Unauthenticated agent run pruning executed in dev mode.")

    deleted = db.prune_agent_runs(retention_days=days)
    return {"status": "success", "deleted_runs": deleted}


@router.post("/agent-runs/{run_id}/toggle-golden")
def toggle_golden_run_endpoint(
    run_id: str,
    request: Request,
):
    """Toggle a run's retention policy between 'standard' and 'golden'."""
    run = db.get_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Enforce owner/admin authorization if auth is enabled
    if auth.auth_enabled():
        user = auth.require_current_user(request)
        is_owner = run.get("user_id") == user["id"]
        # Only the owner or an admin can modify the retention policy
        if not is_owner and not auth.is_admin(user):
            raise HTTPException(status_code=404, detail="Run not found")

    current_policy = run.get("retention_policy", "standard")
    new_policy = "golden" if current_policy == "standard" else "standard"

    success = db.update_agent_run_retention(run_id, new_policy)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update run retention policy")

    return {"status": "success", "retention_policy": new_policy}


@router.post("/agent-runs/{run_id}/postmortem")
def generate_run_postmortem_endpoint(
    run_id: str,
    request: Request,
    force: bool = Query(default=False),
):
    """Generate and return a postmortem report for a completed or failed agent run."""
    run = db.get_agent_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Enforce owner/admin authorization if auth is enabled
    if auth.auth_enabled():
        user = auth.require_current_user(request)
        is_owner = run.get("user_id") == user["id"]
        if not is_owner and not auth.is_admin(user):
            raise HTTPException(status_code=404, detail="Run not found")

    # Check if a postmortem is already cached in the run
    cached_report = run.get("postmortem")
    if cached_report and not force:
        return {"status": "success", "postmortem": cached_report, "cached": True}

    # Generate new postmortem report
    report = postmortem_writer.write_postmortem(run_id)
    if not report:
        raise HTTPException(status_code=500, detail="Failed to generate postmortem report. Make sure LLM is configured.")

    return {"status": "success", "postmortem": report, "cached": False}
