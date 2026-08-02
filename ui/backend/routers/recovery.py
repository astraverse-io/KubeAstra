
"""Recovery / write operation endpoints — all require confirm=True.

POST /api/exec         → exec_pod_command
POST /api/delete-pod   → delete_pod
POST /api/restart      → rollout_restart
POST /api/scale        → scale_deployment
POST /api/patch        → apply_patch
"""

from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from k8s.wrappers import (
    exec_pod_command, delete_pod, rollout_restart, scale_deployment, apply_patch,
)

from http_errors import internal_error

router = APIRouter()


class ExecRequest(BaseModel):
    namespace: str
    pod_name: str
    command: str
    container: Optional[str] = None
    confirm: bool = False


class DeletePodRequest(BaseModel):
    namespace: str
    pod_name: str
    grace_period: int = 30
    confirm: bool = False


class RestartRequest(BaseModel):
    namespace: str
    deployment_name: str
    confirm: bool = False


class ScaleRequest(BaseModel):
    namespace: str
    deployment_name: str
    replicas: int
    confirm: bool = False


class PatchRequest(BaseModel):
    namespace: str
    resource_type: str
    resource_name: str
    patch: str
    patch_type: str = "strategic"
    confirm: bool = False


def _wrap(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        raise internal_error(context="recovery endpoint")


@router.post("/exec")
def api_exec(req: ExecRequest):
    return _wrap(exec_pod_command,
                 req.namespace, req.pod_name, req.command, req.container, req.confirm)


@router.post("/delete-pod")
def api_delete_pod(req: DeletePodRequest):
    return _wrap(delete_pod, req.namespace, req.pod_name, req.grace_period, req.confirm)


@router.post("/restart")
def api_restart(req: RestartRequest):
    return _wrap(rollout_restart, req.namespace, req.deployment_name, req.confirm)


@router.post("/scale")
def api_scale(req: ScaleRequest):
    return _wrap(scale_deployment,
                 req.namespace, req.deployment_name, req.replicas, req.confirm)


@router.post("/patch")
def api_patch(req: PatchRequest):
    return _wrap(apply_patch,
                 req.namespace, req.resource_type, req.resource_name,
                 req.patch, req.patch_type, req.confirm)
