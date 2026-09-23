"""Evidence collectors for Kubernetes objects via get_runner()."""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class EvidenceBundle:
    """Container holding collected Kubernetes JSON objects for analyzer rules."""
    def __init__(self):
        self.pods: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []
        self.nodes: List[Dict[str, Any]] = []
        self.services: List[Dict[str, Any]] = []
        self.endpoints: List[Dict[str, Any]] = []
        self.deployments: List[Dict[str, Any]] = []
        self.pvcs: List[Dict[str, Any]] = []


def _safe_run_json(runner: Any, args: List[str], namespace: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Helper to run kubectl via runner.run_json and catch errors safely."""
    try:
        return runner.run_json(args, namespace=namespace)
    except Exception as exc:
        logger.debug("Evidence collector run_json failed for args %s: %s", args, exc)
        return None


def collect_evidence(
    runner: Any,
    scope_type: str,
    namespace: Optional[str] = None,
    resource_name: Optional[str] = None,
    resource_kind: Optional[str] = None,
) -> EvidenceBundle:
    """Collect scoped evidence from the Kubernetes cluster via the provided runner."""
    bundle = EvidenceBundle()

    # 1. Pods
    if scope_type in ("cluster", "namespace", "pod", "workload"):
        if scope_type == "pod" and resource_name:
            data = _safe_run_json(runner, ["get", "pod", resource_name, "-o", "json"], namespace=namespace)
            if data and data.get("kind") == "Pod":
                bundle.pods.append(data)
        else:
            args = ["get", "pods"]
            if scope_type == "cluster" or namespace == "*":
                args.append("-A")
            data = _safe_run_json(runner, args + ["-o", "json"], namespace=namespace if namespace != "*" else None)
            if data and data.get("kind") == "List":
                bundle.pods.extend(data.get("items", []))

    # 2. Events
    if scope_type in ("cluster", "namespace", "pod", "workload", "node"):
        args = ["get", "events"]
        if scope_type == "cluster" or namespace == "*":
            args.append("-A")
        
        # Filter warning events where possible
        data = _safe_run_json(runner, args + ["-o", "json"], namespace=namespace if namespace != "*" else None)
        if data and data.get("kind") == "List":
            bundle.events.extend(data.get("items", []))

    # 3. Nodes
    if scope_type in ("cluster", "node"):
        if scope_type == "node" and resource_name:
            data = _safe_run_json(runner, ["get", "node", resource_name, "-o", "json"])
            if data and data.get("kind") == "Node":
                bundle.nodes.append(data)
        else:
            data = _safe_run_json(runner, ["get", "nodes", "-o", "json"])
            if data and data.get("kind") == "List":
                bundle.nodes.extend(data.get("items", []))

    # 4. Services
    if scope_type in ("cluster", "namespace", "workload"):
        args = ["get", "services"]
        if scope_type == "cluster" or namespace == "*":
            args.append("-A")
        data = _safe_run_json(runner, args + ["-o", "json"], namespace=namespace if namespace != "*" else None)
        if data and data.get("kind") == "List":
            bundle.services.extend(data.get("items", []))

    # 5. Endpoints / EndpointSlices
    if scope_type in ("cluster", "namespace", "workload"):
        args = ["get", "endpoints"]
        if scope_type == "cluster" or namespace == "*":
            args.append("-A")
        data = _safe_run_json(runner, args + ["-o", "json"], namespace=namespace if namespace != "*" else None)
        if data and data.get("kind") == "List":
            bundle.endpoints.extend(data.get("items", []))

    # 6. Deployments
    if scope_type in ("cluster", "namespace", "workload"):
        args = ["get", "deployments"]
        if scope_type == "cluster" or namespace == "*":
            args.append("-A")
        data = _safe_run_json(runner, args + ["-o", "json"], namespace=namespace if namespace != "*" else None)
        if data and data.get("kind") == "List":
            bundle.deployments.extend(data.get("items", []))

    # 7. PVCs
    if scope_type in ("cluster", "namespace"):
        args = ["get", "pvc"]
        if scope_type == "cluster" or namespace == "*":
            args.append("-A")
        data = _safe_run_json(runner, args + ["-o", "json"], namespace=namespace if namespace != "*" else None)
        if data and data.get("kind") == "List":
            bundle.pvcs.extend(data.get("items", []))

    return bundle
