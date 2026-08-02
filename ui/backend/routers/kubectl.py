
"""Live kubectl endpoints — require kubectl access from the backend host.

POST /api/investigate       → investigate_pod (triage + optional AI)
POST /api/pods              → get_pods
POST /api/describe          → describe_pod
POST /api/logs              → get_pod_logs
POST /api/events            → get_events
POST /api/deployment        → get_deployment
POST /api/service           → get_service
POST /api/endpoints         → get_endpoints
POST /api/rollout-status    → get_rollout_status
POST /api/find              → find_workload
GET  /api/contexts          → list_kubeconfig_contexts
GET  /api/contexts/current  → get_current_context
POST /api/contexts/switch   → switch_kubeconfig_context
POST /api/contexts/add      → add_kubeconfig_context
"""

from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from k8s.wrappers import (
    investigate_pod, get_pods, describe_pod, get_pod_logs,
    get_events, get_deployment, get_service, get_endpoints,
    get_rollout_status, find_workload, list_kubeconfig_contexts,
    get_current_context, switch_kubeconfig_context, add_kubeconfig_context,
)

from http_errors import internal_error

router = APIRouter()


# ── Request models ─────────────────────────────────────────────────────────────

class InvestigateRequest(BaseModel):
    namespace: str
    pod_name: str
    tail: int = 200
    use_ai: bool = True


class PodsRequest(BaseModel):
    namespace: str
    label_selector: Optional[str] = None


class DescribeRequest(BaseModel):
    namespace: str
    pod_name: str


class LogsRequest(BaseModel):
    namespace: str
    pod_name: str
    previous: bool = False
    tail: int = 200
    container: Optional[str] = None


class EventsRequest(BaseModel):
    namespace: str
    field_selector: Optional[str] = None


class DeploymentRequest(BaseModel):
    namespace: str
    deployment_name: str


class ServiceRequest(BaseModel):
    namespace: str
    service_name: str


class EndpointsRequest(BaseModel):
    namespace: str
    service_name: str


class FindWorkloadRequest(BaseModel):
    name: str
    environment: Optional[str] = None


class SwitchContextRequest(BaseModel):
    context_name: str


class AddContextRequest(BaseModel):
    ssh_connection: str
    password: Optional[str] = None
    context_name: Optional[str] = None
    port: int = 22


# ── Endpoints ──────────────────────────────────────────────────────────────────

def _wrap(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        raise internal_error(e, context="kubectl endpoint") from e


@router.post("/investigate")
def api_investigate_pod(req: InvestigateRequest):
    return _wrap(investigate_pod, req.namespace, req.pod_name, req.tail, req.use_ai)


@router.post("/pods")
def api_get_pods(req: PodsRequest):
    return _wrap(get_pods, req.namespace, req.label_selector)


@router.post("/describe")
def api_describe_pod(req: DescribeRequest):
    return _wrap(describe_pod, req.namespace, req.pod_name)


@router.post("/logs")
def api_get_logs(req: LogsRequest):
    return _wrap(get_pod_logs, req.namespace, req.pod_name, req.previous, req.tail, req.container)


@router.post("/events")
def api_get_events(req: EventsRequest):
    return _wrap(get_events, req.namespace, req.field_selector)


@router.post("/deployment")
def api_get_deployment(req: DeploymentRequest):
    return _wrap(get_deployment, req.namespace, req.deployment_name)


@router.post("/service")
def api_get_service(req: ServiceRequest):
    return _wrap(get_service, req.namespace, req.service_name)


@router.post("/endpoints")
def api_get_endpoints(req: EndpointsRequest):
    return _wrap(get_endpoints, req.namespace, req.service_name)


@router.post("/rollout-status")
def api_rollout_status(req: DeploymentRequest):
    return _wrap(get_rollout_status, req.namespace, req.deployment_name)


@router.post("/find")
def api_find_workload(req: FindWorkloadRequest):
    return _wrap(find_workload, req.name, req.environment)


@router.get("/contexts")
def api_list_contexts():
    return _wrap(list_kubeconfig_contexts)


@router.get("/contexts/current")
def api_current_context():
    return _wrap(get_current_context)


@router.post("/contexts/switch")
def api_switch_context(req: SwitchContextRequest):
    return _wrap(switch_kubeconfig_context, req.context_name)


@router.post("/contexts/add")
def api_add_context(req: AddContextRequest):
    return _wrap(add_kubeconfig_context,
                 req.ssh_connection, req.password, req.context_name, req.port)
