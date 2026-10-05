"""Pilots API — list enabled Pilots, start a run, read run state.

Phase 0 wires the framework; it does not yet run an investigation. ``POST
/{name}/run`` records a run row (status ``planning``); Phases 1–2 attach the
deterministic planner (``mcp/services/upgrade``) and the optional ReAct
narration. The whole router is gated by the ``pilots_enabled`` master flag
(404 when off), matching the GitOps router's feature-flag pattern.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import auth
import db
import pilots as pilots_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/pilots", tags=["Pilots"])


def _settings():
    from config.settings import get_settings

    return get_settings()


def _require_enabled() -> None:
    if not getattr(_settings(), "pilots_enabled", False):
        raise HTTPException(status_code=404, detail="Pilots are not enabled")


class PilotRunRequest(BaseModel):
    cluster_id: str = ""
    target: str = ""
    inputs: dict = Field(default_factory=dict)
    explain: bool = False


def _enabled_pilot_or_404(name: str):
    settings = _settings()
    pilot = pilots_registry.get_pilot(name)
    if pilot is None or pilot not in pilots_registry.enabled_pilots(settings):
        raise HTTPException(status_code=404, detail=f"Pilot '{name}' is not available")
    return pilot


@router.get("")
def list_pilots(request: Request) -> dict:
    _require_enabled()
    auth.require_current_user(request)  # no-op when AUTH_ENABLED is off
    # `valid_tool_names` (tool_registry) is the single source of truth for the
    # ReAct surface's tools; resolve the scope against it at the one call site.
    try:
        from tool_registry import valid_tool_names

        valid = set(valid_tool_names("react"))
    except Exception:  # pragma: no cover - registry import is environment-dependent
        valid = set()
    pilots = [
        {
            "name": p.name,
            "description": p.description,
            "write_capable": p.write_capable,
            "tool_scope": pilots_registry.scoped_tool_names(p, valid),
        }
        for p in pilots_registry.enabled_pilots(_settings())
    ]
    return {"pilots": pilots, "count": len(pilots)}


@router.post("/{name}/run", status_code=201)
def start_run(request: Request, name: str, body: PilotRunRequest) -> dict:
    _require_enabled()
    _enabled_pilot_or_404(name)
    # No-op when AUTH_ENABLED is off; enforces identity when it is on.
    user = auth.require_current_user(request)
    created_by = str(user.get("email") or user.get("id")) if user else ""

    if name == "upgrade" and not body.target:
        raise HTTPException(
            status_code=422, detail="the upgrade pilot requires a 'target' version (e.g. '1.31')"
        )

    run_id = str(uuid.uuid4())
    db.create_pilot_run(
        run_id=run_id,
        pilot=name,
        cluster_id=body.cluster_id,
        target=body.target,
        created_by=created_by,
    )
    logger.info("pilot run created: %s (pilot=%s)", run_id, name)
    # Phase 1–2 attach planning here; Phase 0 only records the run.
    return {"run_id": run_id, "pilot": name, "status": "planning"}


@router.get("/runs/{run_id}")
def get_run(request: Request, run_id: str) -> dict:
    _require_enabled()
    auth.require_current_user(request)  # no-op when AUTH_ENABLED is off
    run = db.get_pilot_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run
