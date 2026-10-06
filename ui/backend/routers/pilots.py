"""Pilots API — list enabled Pilots, start a run, read run state.

Phase 0 wires the framework; it does not yet run an investigation. ``POST
/{name}/run`` records a run row (status ``planning``); Phases 1–2 attach the
deterministic planner (``mcp/services/upgrade``) and the optional ReAct
narration. The whole router is gated by the ``pilots_enabled`` master flag
(404 when off), matching the GitOps router's feature-flag pattern.
"""
from __future__ import annotations

import logging
import re
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


def _build_snapshot(body: PilotRunRequest):
    """Build a ClusterSnapshot for the run.

    STATIC (keyless, no cluster) when ``inputs.manifests_path`` is given — the
    CI/Action path. Otherwise a LIVE scan of the default kubectl context via the
    pure ``scan_cluster`` adapter, injecting the real kubectl runner.
    """
    from services.upgrade import load_maps, scan_cluster, scan_manifests

    manifests_path = (body.inputs or {}).get("manifests_path")
    if manifests_path:
        return scan_manifests(_safe_manifests_path(str(manifests_path)))
    from k8s.kubectl_runner import get_runner

    return scan_cluster(get_runner().run_json, load_maps()["deprecations"])


def _safe_manifests_path(raw: str) -> str:
    """Confine a request-supplied manifests path to the configured allowed root.

    STATIC scanning reads whatever path it is given, so letting a request choose
    it would let an authenticated user walk the server's filesystem
    (CodeQL py/path-injection). We require an explicit opt-in root
    (``upgrade_pilot_manifests_root``) and reject anything resolving outside it —
    including absolute paths, ``..`` traversal, and symlink escapes (``resolve()``
    follows links before the containment check). The CLI/Action path never calls
    this; it scans locally.
    """
    from pathlib import Path

    root = (getattr(_settings(), "upgrade_pilot_manifests_root", "") or "").strip()
    if not root:
        raise HTTPException(
            status_code=400,
            detail=(
                "manifests_path scanning is disabled on this server; set "
                "upgrade_pilot_manifests_root to an allowed directory, or omit "
                "manifests_path to scan the live cluster"
            ),
        )
    base = Path(root).resolve()
    candidate = Path(raw)
    target = (candidate if candidate.is_absolute() else base / candidate).resolve()
    if target != base and not target.is_relative_to(base):
        raise HTTPException(status_code=400, detail="manifests_path escapes the allowed root")
    return str(target)


# Argo/Flux names and namespaces flow into kubectl args; validate them so a value
# like "--all" can't be parsed as a flag (argument injection).
_SAFE_K8S_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ALLOWED_GITOPS_KINDS = {"Application", "Kustomization", "HelmRelease"}


def _validate_gitops_live_inputs(inputs: dict) -> None:
    kind = inputs.get("kind", "Application")
    if kind not in _ALLOWED_GITOPS_KINDS:
        raise HTTPException(
            status_code=422, detail=f"inputs.kind must be one of {sorted(_ALLOWED_GITOPS_KINDS)}"
        )
    for field in ("app", "namespace"):
        value = inputs.get(field)
        if value and not _SAFE_K8S_NAME.match(str(value)):
            raise HTTPException(
                status_code=422, detail=f"inputs.{field} is not a valid Kubernetes name"
            )


def _fetch_gitops_object(body: PilotRunRequest) -> dict:
    """The CR to diagnose: ``inputs.object`` when supplied directly (CI / test /
    static), otherwise a LIVE fetch of ``inputs.app`` via the injected runner."""
    inputs = body.inputs or {}
    if isinstance(inputs.get("object"), dict):
        return inputs["object"]
    from k8s.kubectl_runner import get_runner
    from services.reconcile import fetch_gitops_object

    return fetch_gitops_object(
        get_runner().run_json,
        kind=inputs.get("kind", "Application"),
        name=inputs.get("app", ""),
        namespace=inputs.get("namespace", ""),
    )


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
    if name == "gitops_reconcile":
        _inputs = body.inputs or {}
        if isinstance(_inputs.get("object"), dict):
            pass  # static/CI/test: diagnose the supplied CR as-is (data, not kubectl args)
        elif _inputs.get("app"):
            _validate_gitops_live_inputs(_inputs)  # live path: these become kubectl args
        else:
            raise HTTPException(
                status_code=422,
                detail="gitops_reconcile requires inputs.app (the Argo/Flux app name) or inputs.object (the CR)",
            )

    run_id = str(uuid.uuid4())
    db.create_pilot_run(
        run_id=run_id,
        pilot=name,
        cluster_id=body.cluster_id,
        target=body.target,
        created_by=created_by,
    )

    if name == "gitops_reconcile":
        # Deterministic, keyless diagnosis of an Argo/Flux app.
        try:
            from services.reconcile import diagnose

            dx = diagnose(_fetch_gitops_object(body))
            result = {"diagnosis": dx.to_dict()}
            db.update_pilot_run(run_id, status="diagnosed", plan=result)
        except Exception as exc:
            logger.exception("pilot run %s failed", run_id)
            db.update_pilot_run(run_id, status="failed", plan={"error": str(exc)})
            raise HTTPException(status_code=500, detail=f"pilot run failed: {exc}")
        logger.info("pilot run %s diagnosed (%s)", run_id, dx.root_cause)
        return {"run_id": run_id, "pilot": name, "status": "diagnosed", **result}

    if name == "upgrade":
        # Deterministic, keyless: scan -> assess -> plan. Narration (explain=true)
        # is a later, optional overlay; the plan itself never needs an LLM.
        try:
            from services.upgrade import assess, load_maps, plan

            maps = load_maps()
            report = assess(_build_snapshot(body), body.target, maps)
            migration = plan(report, maps)
            result = {"report": report.to_dict(), "plan": migration.to_dict()}
            db.update_pilot_run(run_id, status="planned", plan=result)
        except Exception as exc:  # surface a failed run rather than a silent 500
            logger.exception("pilot run %s failed", run_id)
            db.update_pilot_run(run_id, status="failed", plan={"error": str(exc)})
            raise HTTPException(status_code=500, detail=f"pilot run failed: {exc}")
        logger.info("pilot run %s planned (%d steps)", run_id, len(migration.steps))
        return {"run_id": run_id, "pilot": name, "status": "planned", **result}

    # A registered + enabled pilot with no run implementation yet (e.g. a backlog
    # pilot). Record it, but never mis-route into another pilot's pipeline.
    db.update_pilot_run(run_id, status="not_implemented")
    raise HTTPException(status_code=501, detail=f"pilot '{name}' has no run implementation yet")


@router.get("/runs/{run_id}")
def get_run(request: Request, run_id: str) -> dict:
    _require_enabled()
    auth.require_current_user(request)  # no-op when AUTH_ENABLED is off
    run = db.get_pilot_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run
