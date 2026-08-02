"""High-level kubectl wrappers for investigation operations."""

import hashlib
import logging
import subprocess
import re
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from k8s import redaction
from k8s.kubectl_runner import get_runner, KubectlError
from k8s.parsers import (
    parse_deployment,
    parse_endpoints,
    parse_endpoint_slices,
    parse_events,
    parse_pod_describe_highlights,
    parse_pod_list,
    parse_service,
    truncate_logs,
)
from k8s.validators import (
    validate_environment_hint,
    validate_label_selector,
    validate_namespace,
    validate_node_name,
    validate_resource_name,
    validate_tail_lines,
    get_allowed_namespaces,
)
from config.settings import settings

logger = logging.getLogger(__name__)

_ai_service_available = True
try:
    from services.llm_service import llm_service as _llm_service
except ImportError:
    _ai_service_available = False
    _llm_service = None

# Store deployment repo in workspace to avoid permission issues
DEPLOYMENT_REPO_PATH = Path(__file__).parent.parent / ".deployment-provisioning-cache"


def find_workload(
    name: str,
    environment: Optional[str] = None
) -> Dict[str, Any]:
    """
    Search for matching workloads across allowed namespaces.

    When ALLOWED_NAMESPACES=* (wildcard), uses a single --all-namespaces
    kubectl call per resource type (3 calls total) instead of N calls per
    namespace, which avoids multi-minute hangs on large clusters.

    Args:
        name: Workload name or partial name to search for
        environment: Optional environment hint (prod, staging, dev)

    Returns:
        Dict with matches grouped by namespace and resource type
    """
    validate_resource_name(name, "workload")
    environment = validate_environment_hint(environment)

    allowed_namespaces = get_allowed_namespaces()
    wildcard = "*" in allowed_namespaces

    matches: Dict[str, Any] = {
        "query": name,
        "environment_hint": environment,
        "deployments": [],
        "pods": [],
        "services": [],
    }

    def _search_all_namespaces(resource: str) -> list:
        """Single --all-namespaces call — fast even on large clusters."""
        try:
            result = get_runner().run_json(["get", resource, "--all-namespaces", "-o", "json"])
            return result.get("items", [])
        except Exception as e:
            logger.warning(f"Error fetching {resource} across all namespaces: {e}")
            return []

    def _search_per_namespace(resource: str, namespaces: list) -> list:
        """Per-namespace calls — used when a specific namespace list is given."""
        items = []
        for ns in namespaces:
            try:
                result = get_runner().run_json(["get", resource, "-o", "json"], namespace=ns)
                for item in result.get("items", []):
                    item["_searched_ns"] = ns
                items.extend(result.get("items", []))
            except KubectlError as e:
                logger.warning(f"Error searching {resource} in {ns}: {e}")
        return items

    if wildcard:
        all_deployments = _search_all_namespaces("deployments")
        all_pods = _search_all_namespaces("pods")
        all_services = _search_all_namespaces("services")
        namespaces_searched = "all"
    else:
        if environment:
            prioritized = [ns for ns in allowed_namespaces if environment in ns.lower()]
            rest = [ns for ns in allowed_namespaces if environment not in ns.lower()]
            search_namespaces = prioritized + rest
        else:
            search_namespaces = allowed_namespaces
        all_deployments = _search_per_namespace("deployments", search_namespaces)
        all_pods = _search_per_namespace("pods", search_namespaces)
        all_services = _search_per_namespace("services", search_namespaces)
        namespaces_searched = len(search_namespaces)

    # Filter by name match
    for item in all_deployments:
        item_name = item.get("metadata", {}).get("name", "")
        if name.lower() in item_name.lower():
            matches["deployments"].append({
                "namespace": item.get("metadata", {}).get("namespace", ""),
                "name": item_name,
                "replicas": item.get("spec", {}).get("replicas"),
                "ready": item.get("status", {}).get("readyReplicas", 0),
            })

    for item in all_pods:
        item_name = item.get("metadata", {}).get("name", "")
        if name.lower() in item_name.lower():
            pod_status = item.get("status", {})
            init_container_statuses = pod_status.get("initContainerStatuses", [])
            container_statuses = pod_status.get("containerStatuses", [])
            all_container_statuses = [*init_container_statuses, *container_statuses]
            restarts = sum(cs.get("restartCount", 0) for cs in all_container_statuses)
            ready_count = sum(1 for cs in container_statuses if cs.get("ready", False))
            total_count = len(container_statuses) if container_statuses else 0
            # Derive effective status (check container waiting reasons)
            effective_status = pod_status.get("phase", "Unknown")
            for cs in all_container_statuses:
                waiting = cs.get("state", {}).get("waiting", {})
                reason = waiting.get("reason", "")
                if reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
                              "CreateContainerConfigError", "OOMKilled"):
                    effective_status = reason
                    break
                terminated = cs.get("state", {}).get("terminated", {})
                t_reason = terminated.get("reason", "")
                if t_reason in ("OOMKilled", "Error") and restarts > 0:
                    effective_status = t_reason
                    break
            containers = item.get("spec", {}).get("containers", [])
            matches["pods"].append({
                "namespace": item.get("metadata", {}).get("namespace", ""),
                "name": item_name,
                "phase": pod_status.get("phase", "Unknown"),
                "status": effective_status,
                "ready": f"{ready_count}/{total_count}",
                "restarts": restarts,
                "image": containers[0].get("image", "") if containers else "",
                "node": item.get("spec", {}).get("nodeName", ""),
            })

    for item in all_services:
        item_name = item.get("metadata", {}).get("name", "")
        if name.lower() in item_name.lower():
            matches["services"].append({
                "namespace": item.get("metadata", {}).get("namespace", ""),
                "name": item_name,
                "type": item.get("spec", {}).get("type", ""),
            })

    matches["summary"] = {
        "total_deployments": len(matches["deployments"]),
        "total_pods": len(matches["pods"]),
        "total_services": len(matches["services"]),
        "namespaces_searched": namespaces_searched,
    }

    return matches


def get_namespaces() -> Dict[str, Any]:
    """List all namespaces in the cluster with their status."""
    result = get_runner().run_json(["get", "namespaces", "-o", "json"])
    items = result.get("items", [])
    namespaces = []
    for ns in items:
        name = ns.get("metadata", {}).get("name", "")
        phase = ns.get("status", {}).get("phase", "Unknown")
        labels = ns.get("metadata", {}).get("labels", {})
        namespaces.append({"name": name, "status": phase, "labels": labels})
    namespaces.sort(key=lambda n: n["name"])
    return {
        "namespace_count": len(namespaces),
        "namespaces": namespaces,
    }


def _metadata_annotations_summary(metadata: dict) -> dict:
    annotations = metadata.get("annotations", {}) or {}
    return {
        "count": len(annotations),
        "keys": sorted(annotations.keys()),
    }


def _node_conditions(status: dict) -> list[dict]:
    return [
        {
            "type": cond.get("type", ""),
            "status": cond.get("status", ""),
            "reason": cond.get("reason", ""),
            "message": cond.get("message", ""),
            "last_heartbeat_time": cond.get("lastHeartbeatTime", ""),
            "last_transition_time": cond.get("lastTransitionTime", ""),
        }
        for cond in status.get("conditions", []) or []
        if isinstance(cond, dict)
    ]


def _node_addresses(status: dict) -> list[dict]:
    return [
        {"type": addr.get("type", ""), "address": addr.get("address", "")}
        for addr in status.get("addresses", []) or []
        if isinstance(addr, dict)
    ]


def _node_taints(spec: dict) -> list[dict]:
    return [
        {
            "key": taint.get("key", ""),
            "value": taint.get("value", ""),
            "effect": taint.get("effect", ""),
            "time_added": taint.get("timeAdded", ""),
        }
        for taint in spec.get("taints", []) or []
        if isinstance(taint, dict)
    ]


def get_nodes(
    node_name: Optional[str] = None,
    labels_only: bool = False,
    taints_only: bool = False,
    conditions_only: bool = False,
    addresses_only: bool = False,
) -> Dict[str, Any]:
    """List all nodes with status/resources/labels, or inspect one node by name."""
    if node_name:
        return investigate_node(node_name)

    result = get_runner().run_json(["get", "nodes", "-o", "json"])
    nodes = []
    active_modes = [
        name for name, enabled in {
            "labels": labels_only,
            "taints": taints_only,
            "conditions": conditions_only,
            "addresses": addresses_only,
        }.items() if enabled
    ]
    for node in result.get("items", []):
        metadata = node.get("metadata", {}) or {}
        spec = node.get("spec", {}) or {}
        status = node.get("status", {})
        capacity = status.get("capacity", {}) or {}
        allocatable = status.get("allocatable", {}) or {}
        ready = "Unknown"
        for cond in status.get("conditions", []):
            if cond.get("type") == "Ready":
                ready = "Ready" if cond.get("status") == "True" else "NotReady"
                break

        info = status.get("nodeInfo", {})
        labels = metadata.get("labels", {}) or {}
        if labels_only:
            nodes.append({
                "name": metadata.get("name", ""),
                "labels": labels,
                "label_count": len(labels),
            })
            continue
        if taints_only:
            nodes.append({
                "name": metadata.get("name", ""),
                "taints": _node_taints(spec),
                "unschedulable": spec.get("unschedulable", False),
            })
            continue
        if conditions_only:
            nodes.append({
                "name": metadata.get("name", ""),
                "status": ready,
                "conditions": _node_conditions(status),
            })
            continue
        if addresses_only:
            nodes.append({
                "name": metadata.get("name", ""),
                "addresses": _node_addresses(status),
            })
            continue

        nodes.append({
            "name": metadata.get("name", ""),
            "status": ready,
            "roles": _node_roles(labels),
            "labels": labels,
            "label_count": len(labels),
            "annotations": _metadata_annotations_summary(metadata),
            "taints": _node_taints(spec),
            "unschedulable": spec.get("unschedulable", False),
            "addresses": _node_addresses(status),
            "conditions": _node_conditions(status),
            "version": info.get("kubeletVersion", ""),
            "os_image": info.get("osImage", ""),
            "capacity": {
                "cpu": capacity.get("cpu", ""),
                "memory": capacity.get("memory", ""),
                "pods": capacity.get("pods", ""),
            },
            "allocatable": {
                "cpu": allocatable.get("cpu", ""),
                "memory": allocatable.get("memory", ""),
                "pods": allocatable.get("pods", ""),
            },
        })

    if labels_only and active_modes == ["labels"]:
        return {
            "node_count": len(nodes),
            "labels_only": True,
            "nodes": nodes,
        }

    return {
        "node_count": len(nodes),
        "labels_only": labels_only,
        "taints_only": taints_only,
        "conditions_only": conditions_only,
        "addresses_only": addresses_only,
        "focused_modes": active_modes,
        "nodes": nodes,
    }


def investigate_node(node_name: str) -> Dict[str, Any]:
    """Inspect one node's capacity, allocatable resources, and pod allocations."""
    validate_node_name(node_name)

    nodes_json = get_runner().run_json(["get", "nodes", "-o", "json"])
    resolved = _resolve_node_from_list(node_name, nodes_json.get("items", []) or [])
    if resolved.get("error"):
        return resolved

    node_obj = resolved["node"]
    metadata = node_obj.get("metadata", {}) or {}
    node_spec = node_obj.get("spec", {}) or {}
    resolved_node_name = metadata.get("name", node_name)
    status = node_obj.get("status", {}) or {}
    capacity = status.get("capacity", {}) or {}
    allocatable = status.get("allocatable", {}) or {}
    labels = metadata.get("labels", {}) or {}

    ready = "Unknown"
    conditions = []
    for cond in status.get("conditions", []) or []:
        cond_summary = {
            "type": cond.get("type", ""),
            "status": cond.get("status", ""),
            "reason": cond.get("reason", ""),
            "message": cond.get("message", ""),
            "last_heartbeat_time": cond.get("lastHeartbeatTime", ""),
            "last_transition_time": cond.get("lastTransitionTime", ""),
        }
        conditions.append(cond_summary)
        if cond.get("type") == "Ready":
            ready = "Ready" if cond.get("status") == "True" else "NotReady"

    pods_json = get_runner().run_json(["get", "pods", "--all-namespaces", "-o", "json"])
    scheduled_pods = []
    cpu_requests_m = 0
    cpu_limits_m = 0
    memory_requests_bytes = 0
    memory_limits_bytes = 0

    for pod in pods_json.get("items", []) or []:
        spec = pod.get("spec", {}) or {}
        if spec.get("nodeName") != resolved_node_name:
            continue
        phase = (pod.get("status", {}) or {}).get("phase", "")
        if phase in ("Succeeded", "Failed"):
            continue

        pod_totals = _pod_resource_totals(spec)
        pod_cpu_requests_m = pod_totals["cpu_requests_millicores"]
        pod_cpu_limits_m = pod_totals["cpu_limits_millicores"]
        pod_memory_requests_bytes = pod_totals["memory_requests_bytes"]
        pod_memory_limits_bytes = pod_totals["memory_limits_bytes"]

        cpu_requests_m += pod_cpu_requests_m
        cpu_limits_m += pod_cpu_limits_m
        memory_requests_bytes += pod_memory_requests_bytes
        memory_limits_bytes += pod_memory_limits_bytes
        meta = pod.get("metadata", {}) or {}
        scheduled_pods.append({
            "namespace": meta.get("namespace", ""),
            "name": meta.get("name", ""),
            "phase": phase,
            "cpu_requests_millicores": pod_cpu_requests_m,
            "cpu_limits_millicores": pod_cpu_limits_m,
            "memory_requests_bytes": pod_memory_requests_bytes,
            "memory_requests_gib": _bytes_to_gib(pod_memory_requests_bytes),
            "memory_limits_bytes": pod_memory_limits_bytes,
            "memory_limits_gib": _bytes_to_gib(pod_memory_limits_bytes),
        })

    allocatable_cpu_m = _parse_cpu_millicores(allocatable.get("cpu"))
    capacity_cpu_m = _parse_cpu_millicores(capacity.get("cpu"))
    allocatable_memory_bytes = _parse_memory_bytes(allocatable.get("memory"))
    capacity_memory_bytes = _parse_memory_bytes(capacity.get("memory"))

    return {
        "name": resolved_node_name,
        "query": node_name,
        "status": ready,
        "roles": _node_roles(labels),
        "labels": labels,
        "label_count": len(labels),
        "annotations": _metadata_annotations_summary(metadata),
        "taints": _node_taints(node_spec),
        "unschedulable": node_spec.get("unschedulable", False),
        "addresses": _node_addresses(status),
        "capacity": {
            "cpu": capacity.get("cpu", ""),
            "cpu_millicores": capacity_cpu_m,
            "memory": capacity.get("memory", ""),
            "memory_bytes": capacity_memory_bytes,
            "memory_gib": _bytes_to_gib(capacity_memory_bytes),
            "pods": capacity.get("pods", ""),
        },
        "allocatable": {
            "cpu": allocatable.get("cpu", ""),
            "cpu_millicores": allocatable_cpu_m,
            "memory": allocatable.get("memory", ""),
            "memory_bytes": allocatable_memory_bytes,
            "memory_gib": _bytes_to_gib(allocatable_memory_bytes),
            "pods": allocatable.get("pods", ""),
        },
        "allocated": {
            "cpu_requests_millicores": cpu_requests_m,
            "cpu_requests_cores": round(cpu_requests_m / 1000, 3),
            "cpu_requests_percent_of_allocatable": _percent(cpu_requests_m, allocatable_cpu_m),
            "cpu_limits_millicores": cpu_limits_m,
            "cpu_limits_cores": round(cpu_limits_m / 1000, 3),
            "cpu_limits_percent_of_allocatable": _percent(cpu_limits_m, allocatable_cpu_m),
            "memory_requests_bytes": memory_requests_bytes,
            "memory_requests_gib": _bytes_to_gib(memory_requests_bytes),
            "memory_requests_percent_of_allocatable": _percent(memory_requests_bytes, allocatable_memory_bytes),
            "memory_limits_bytes": memory_limits_bytes,
            "memory_limits_gib": _bytes_to_gib(memory_limits_bytes),
            "memory_limits_percent_of_allocatable": _percent(memory_limits_bytes, allocatable_memory_bytes),
            "non_terminated_pods": len(scheduled_pods),
        },
        "conditions": conditions,
        "pods": scheduled_pods,
    }


def _node_roles(labels: dict) -> list[str]:
    roles = []
    prefix = "node-role.kubernetes.io/"
    for key in labels:
        if key.startswith(prefix):
            role = key[len(prefix):] or "control-plane"
            roles.append(role)
    return sorted(roles) or ["worker"]


def _resolve_node_from_list(query: str, nodes: list[dict]) -> Dict[str, Any]:
    """Resolve exact node names first, then a single safe partial/FQDN match."""
    query_l = query.lower()
    names = [n.get("metadata", {}).get("name", "") for n in nodes]

    for node, name in zip(nodes, names):
        if name.lower() == query_l:
            return {"node": node}

    matches = [
        (node, name)
        for node, name in zip(nodes, names)
        if query_l in name.lower()
    ]
    if len(matches) == 1:
        return {"node": matches[0][0], "matched_name": matches[0][1]}

    if len(matches) > 1:
        return {
            "error": "ambiguous_node_name",
            "message": f"Node name '{query}' matched multiple nodes. Please use the full node name.",
            "query": query,
            "matches": [name for _, name in matches],
        }

    return {
        "error": "node_not_found",
        "message": f"Node '{query}' was not found in the current cluster.",
        "query": query,
        "available_nodes": names[:50],
        "node_count": len(names),
    }


def _pod_resource_totals(spec: dict) -> Dict[str, int]:
    """Return pod resource totals using scheduler-style init container handling."""
    app = _container_resource_totals(spec.get("containers", []) or [])
    init_totals = [
        _container_resource_totals([container])
        for container in spec.get("initContainers", []) or []
    ]

    def _max_init(key: str) -> int:
        return max((item[key] for item in init_totals), default=0)

    return {
        "cpu_requests_millicores": max(app["cpu_requests_millicores"], _max_init("cpu_requests_millicores")),
        "cpu_limits_millicores": max(app["cpu_limits_millicores"], _max_init("cpu_limits_millicores")),
        "memory_requests_bytes": max(app["memory_requests_bytes"], _max_init("memory_requests_bytes")),
        "memory_limits_bytes": max(app["memory_limits_bytes"], _max_init("memory_limits_bytes")),
    }


def _container_resource_totals(containers: list[dict]) -> Dict[str, int]:
    totals = {
        "cpu_requests_millicores": 0,
        "cpu_limits_millicores": 0,
        "memory_requests_bytes": 0,
        "memory_limits_bytes": 0,
    }
    for container in containers:
        resources = container.get("resources", {}) or {}
        requests = resources.get("requests", {}) or {}
        limits = resources.get("limits", {}) or {}
        totals["cpu_requests_millicores"] += _parse_cpu_millicores(requests.get("cpu"))
        totals["cpu_limits_millicores"] += _parse_cpu_millicores(limits.get("cpu"))
        totals["memory_requests_bytes"] += _parse_memory_bytes(requests.get("memory"))
        totals["memory_limits_bytes"] += _parse_memory_bytes(limits.get("memory"))
    return totals


def _parse_cpu_millicores(value: Any) -> int:
    if value is None:
        return 0
    raw = str(value).strip()
    if not raw:
        return 0
    try:
        if raw.endswith("m"):
            return int(float(raw[:-1]))
        if raw.endswith("u"):
            return int(float(raw[:-1]) / 1000)
        if raw.endswith("n"):
            return int(float(raw[:-1]) / 1_000_000)
        return int(float(raw) * 1000)
    except ValueError:
        return 0


def _parse_memory_bytes(value: Any) -> int:
    if value is None:
        return 0
    raw = str(value).strip()
    if not raw:
        return 0

    binary_units = {
        "Ki": 1024,
        "Mi": 1024 ** 2,
        "Gi": 1024 ** 3,
        "Ti": 1024 ** 4,
        "Pi": 1024 ** 5,
        "Ei": 1024 ** 6,
        "ki": 1024,
        "mi": 1024 ** 2,
        "gi": 1024 ** 3,
        "ti": 1024 ** 4,
        "pi": 1024 ** 5,
        "ei": 1024 ** 6,
    }
    decimal_units = {
        "n": 1e-9,
        "u": 1e-6,
        "m": 1e-3,
        "k": 1000,
        "K": 1000,
        "M": 1000 ** 2,
        "G": 1000 ** 3,
        "T": 1000 ** 4,
        "P": 1000 ** 5,
        "E": 1000 ** 6,
    }

    for suffix, multiplier in binary_units.items():
        if raw.endswith(suffix):
            try:
                return int(float(raw[:-len(suffix)]) * multiplier)
            except ValueError:
                return 0
    for suffix, multiplier in decimal_units.items():
        if raw.endswith(suffix):
            try:
                return int(float(raw[:-len(suffix)]) * multiplier)
            except ValueError:
                return 0
    try:
        return int(float(raw))
    except ValueError:
        return 0


def _bytes_to_gib(value: int) -> float:
    if value <= 0:
        return 0.0
    return round(value / (1024 ** 3), 3)


def _percent(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100, 2)


def list_namespace_resources(namespace: str) -> Dict[str, Any]:
    """List all key resource types in a namespace in one call.

    Returns pods, services, deployments, statefulsets, daemonsets,
    configmaps, PVCs, and ingresses — giving a full picture of what is
    running in the namespace without needing multiple queries.
    """
    namespace = validate_namespace(namespace)

    def _fetch(resource: str) -> List[dict]:
        try:
            data = get_runner().run_json(["get", resource, "-o", "json"], namespace=namespace)
            return data.get("items", [])
        except Exception:
            return []

    def _meta(item: dict) -> dict:
        m = item.get("metadata", {})
        labels = m.get("labels", {}) or {}
        return {
            "name": m.get("name", ""),
            "namespace": m.get("namespace", namespace),
            "labels": labels,
            "label_count": len(labels),
        }

    def _container_summaries(template: dict) -> list[dict]:
        spec = template.get("spec", {}) if isinstance(template, dict) else {}
        return [
            {
                "name": c.get("name", ""),
                "image": c.get("image", ""),
                "resources": c.get("resources", {}) or {},
            }
            for c in spec.get("containers", []) or []
            if isinstance(c, dict)
        ]

    def _template_summary(spec: dict) -> dict:
        template = spec.get("template", {}) if isinstance(spec, dict) else {}
        metadata = template.get("metadata", {}) if isinstance(template, dict) else {}
        labels = metadata.get("labels", {}) or {}
        containers = _container_summaries(template)
        return {
            "labels": labels,
            "label_count": len(labels),
            "images": [c["image"] for c in containers if c.get("image")],
            "containers": containers,
        }

    def _backend_summary(backend: dict) -> dict:
        service = backend.get("service", {}) if isinstance(backend, dict) else {}
        port = service.get("port", {}) if isinstance(service, dict) else {}
        return {
            "service": service.get("name", ""),
            "port": port.get("number") if "number" in port else port.get("name", ""),
        }

    # Pods
    raw_pods = _fetch("pods")
    pods = []
    for p in raw_pods:
        meta = _meta(p)
        spec = p.get("spec", {})
        status_obj = p.get("status", {})
        phase = status_obj.get("phase", "Unknown")
        conditions = {c["type"]: c["status"] for c in status_obj.get("conditions", [])}
        ready = conditions.get("Ready", "False") == "True"
        restarts = sum(
            cs.get("restartCount", 0)
            for cs in status_obj.get("containerStatuses", [])
        )
        pods.append({
            **meta,
            "status": phase,
            "ready": ready,
            "restarts": restarts,
            "node_name": spec.get("nodeName", ""),
            "images": [
                c.get("image", "")
                for c in spec.get("containers", []) or []
                if isinstance(c, dict) and c.get("image")
            ],
        })

    # Services
    raw_svcs = _fetch("services")
    services = []
    for s in raw_svcs:
        service = parse_service(s)
        # Keep old aliases for callers that still read list inventory output.
        service["port_details"] = [
            {
                **{
                    "name": p.get("name", ""),
                    "protocol": p.get("protocol", "TCP"),
                    "port": p.get("port"),
                    "target_port": p.get("target_port"),
                },
                **({"node_port": p.get("node_port")} if "node_port" in p else {}),
            }
            for p in service.get("ports", []) or []
            if isinstance(p, dict)
        ]
        service["port_strings"] = [
            f"{p.get('port')}/{p.get('protocol', 'TCP')}"
            for p in service.get("ports", []) or []
            if isinstance(p, dict)
        ]
        services.append(service)

    # Deployments
    raw_deps = _fetch("deployments")
    deployments = []
    for d in raw_deps:
        meta = _meta(d)
        spec = d.get("spec", {})
        status = d.get("status", {})
        template = _template_summary(spec)
        deployments.append({
            **meta,
            "replicas": spec.get("replicas", 0),
            "ready": status.get("readyReplicas", 0),
            "available": status.get("availableReplicas", 0),
            "updated": status.get("updatedReplicas", 0),
            "unavailable": status.get("unavailableReplicas", 0),
            "selector": spec.get("selector", {}) or {},
            "images": template["images"],
            "containers": template["containers"],
            "template_labels": template["labels"],
        })

    # StatefulSets
    raw_sts = _fetch("statefulsets")
    statefulsets = []
    for s in raw_sts:
        meta = _meta(s)
        spec = s.get("spec", {})
        status = s.get("status", {})
        template = _template_summary(spec)
        statefulsets.append({
            **meta,
            "service_name": spec.get("serviceName", ""),
            "replicas": spec.get("replicas", 0),
            "ready": status.get("readyReplicas", 0),
            "current": status.get("currentReplicas", 0),
            "updated": status.get("updatedReplicas", 0),
            "selector": spec.get("selector", {}) or {},
            "images": template["images"],
            "containers": template["containers"],
            "template_labels": template["labels"],
        })

    # DaemonSets
    raw_ds = _fetch("daemonsets")
    daemonsets = []
    for d in raw_ds:
        meta = _meta(d)
        spec = d.get("spec", {})
        status = d.get("status", {})
        template = _template_summary(spec)
        daemonsets.append({
            **meta,
            "desired": status.get("desiredNumberScheduled", 0),
            "ready": status.get("numberReady", 0),
            "available": status.get("numberAvailable", 0),
            "updated": status.get("updatedNumberScheduled", 0),
            "selector": spec.get("selector", {}) or {},
            "images": template["images"],
            "containers": template["containers"],
            "template_labels": template["labels"],
        })

    # ConfigMaps (exclude system ones)
    raw_cms = _fetch("configmaps")
    configmaps = [
        _meta(c) for c in raw_cms
        if _meta(c)["name"] not in ("kube-root-ca.crt",)
    ]

    # PersistentVolumeClaims
    raw_pvcs = _fetch("persistentvolumeclaims")
    persistent_volume_claims = []
    for pvc in raw_pvcs:
        meta = _meta(pvc)
        spec = pvc.get("spec", {})
        status = pvc.get("status", {})
        persistent_volume_claims.append({
            **meta,
            "status": status.get("phase", ""),
            "storage_class_name": spec.get("storageClassName", ""),
            "access_modes": spec.get("accessModes", []) or [],
            "capacity": status.get("capacity", {}) or {},
            "volume_name": spec.get("volumeName", ""),
        })

    # Ingresses
    raw_ing = _fetch("ingresses")
    ingresses = []
    for i in raw_ing:
        meta = _meta(i)
        spec = i.get("spec", {})
        rules = spec.get("rules", [])
        hosts = [r.get("host", "") for r in rules if r.get("host")]
        rule_summaries = []
        for rule in rules:
            http = rule.get("http", {}) if isinstance(rule, dict) else {}
            paths = []
            for path in http.get("paths", []) or []:
                if not isinstance(path, dict):
                    continue
                paths.append({
                    "path": path.get("path", ""),
                    "path_type": path.get("pathType", ""),
                    "backend": _backend_summary(path.get("backend", {})),
                })
            rule_summaries.append({"host": rule.get("host", ""), "paths": paths})
        ingresses.append({
            **meta,
            "hosts": hosts,
            "rules": rule_summaries,
            "default_backend": _backend_summary(spec.get("defaultBackend", {})) if spec.get("defaultBackend") else None,
        })

    return {
        "namespace": namespace,
        "pods": pods,
        "services": services,
        "deployments": deployments,
        "statefulsets": statefulsets,
        "daemonsets": daemonsets,
        "configmaps": configmaps,
        "persistent_volume_claims": persistent_volume_claims,
        "ingresses": ingresses,
        "summary": {
            "pods": len(pods),
            "services": len(services),
            "deployments": len(deployments),
            "statefulsets": len(statefulsets),
            "daemonsets": len(daemonsets),
            "configmaps": len(configmaps),
            "persistent_volume_claims": len(persistent_volume_claims),
            "ingresses": len(ingresses),
        },
    }


# ── ConfigMap inspection (read-only, redacted) ───────────────────────────────
#
# list_namespace_resources deliberately omits ConfigMap *data*. These two tools
# expose that data for source/config tracing, with redaction + size caps.
# Secret values are intentionally out of scope and are never read here.

# Per-value and total output caps so a large ConfigMap can't flood the agent.
_CM_VALUE_CAP = 4096          # bytes per returned value
_CM_PREVIEW_CAP = 200         # bytes per key preview when no key is requested
_CM_TOTAL_CAP = 16384         # bytes across all returned values
_CM_EXCERPT_CAP = 200         # bytes per search match excerpt
_CM_DEFAULT_MAX_MATCHES = 20
_CM_MAX_MATCHES_CAP = 50      # hard ceiling regardless of caller-supplied value

# Redaction logic is shared with the Helm tools — see k8s/redaction.py.
_cm_truncate = redaction.truncate
_redact_cm_line = redaction.redact_line
_redact_cm_value = redaction.redact_value


def _cm_labels_hint(metadata: dict) -> dict:
    """Surface the labels/annotations most useful for source tracing."""
    labels = metadata.get("labels", {}) or {}
    annotations = metadata.get("annotations", {}) or {}
    hint = {}
    for k in ("helm.sh/chart", "app.kubernetes.io/managed-by", "app.kubernetes.io/name"):
        if labels.get(k):
            hint[k] = labels[k]
    tracking = annotations.get("argocd.argoproj.io/tracking-id")
    if tracking:
        hint["argocd.argoproj.io/tracking-id"] = tracking
    return hint


def get_configmap(namespace: str, name: str, key: Optional[str] = None) -> Dict[str, Any]:
    """Read a single ConfigMap's data (read-only, redacted, size-capped).

    When ``key`` is given, returns that key's redacted/capped value. When no
    key is given, returns the key list plus small redacted previews — never the
    full values — so the agent can decide which key to read next.
    """
    namespace = validate_namespace(namespace)
    name = validate_resource_name(name)

    try:
        data = get_runner().run_json(
            ["get", "configmap", name, "-o", "json"], namespace=namespace
        )
    except KubectlError as exc:
        return {"found": False, "namespace": namespace, "name": name, "error": str(exc)}
    except Exception as exc:  # pragma: no cover - defensive
        return {"found": False, "namespace": namespace, "name": name, "error": str(exc)}

    if not isinstance(data, dict) or not data.get("metadata"):
        return {"found": False, "namespace": namespace, "name": name}

    metadata = data.get("metadata", {}) or {}
    cm_data = data.get("data", {}) or {}
    keys = sorted(cm_data.keys())
    result: Dict[str, Any] = {
        "found": True,
        "namespace": namespace,
        "name": name,
        "keys": keys,
        "labels_hint": _cm_labels_hint(metadata),
    }

    if key is not None:
        if key not in cm_data:
            result["error"] = f"key '{key}' not found in ConfigMap"
            return result
        result["key"] = key
        result["value"] = _redact_cm_value(key, str(cm_data[key]), _CM_VALUE_CAP)
        return result

    # No specific key: previews only, never full values.
    previews = {}
    budget = _CM_TOTAL_CAP
    for k in keys:
        if budget <= 0:
            previews[k] = "… [omitted, output budget reached]"
            continue
        preview = _redact_cm_value(k, str(cm_data[k]), min(_CM_PREVIEW_CAP, budget))
        previews[k] = preview
        budget -= len(preview)
    result["previews"] = previews
    return result


def search_configmaps(
    namespace: str, query: str, max_matches: Optional[int] = None
) -> Dict[str, Any]:
    """Find which ConfigMap(s) in a namespace contain a value or key.

    Scans every ConfigMap's keys and values for ``query`` (case-insensitive
    substring) and returns the owning ConfigMap, key, and a redacted excerpt.
    This is the discovery path: the agent knows a failing value (e.g. a pinned
    version) but not where it is defined.
    """
    namespace = validate_namespace(namespace)
    q = (query or "").strip()
    if not q:
        return {"namespace": namespace, "query": query, "searched_count": 0,
                "match_count": 0, "matches": [], "error": "empty query"}
    requested = max_matches if isinstance(max_matches, int) and max_matches > 0 else _CM_DEFAULT_MAX_MATCHES
    limit = min(requested, _CM_MAX_MATCHES_CAP)  # hard cap regardless of caller
    q_low = q.lower()

    try:
        data = get_runner().run_json(["get", "configmaps", "-o", "json"], namespace=namespace)
        items = data.get("items", []) if isinstance(data, dict) else []
    except Exception as exc:  # pragma: no cover - defensive
        return {"namespace": namespace, "query": query, "searched_count": 0,
                "match_count": 0, "matches": [], "error": str(exc)}

    matches: List[dict] = []
    truncated = False
    for item in items:
        if len(matches) >= limit:
            truncated = True
            break
        metadata = item.get("metadata", {}) or {}
        cm_name = metadata.get("name", "")
        labels_hint = _cm_labels_hint(metadata)
        cm_data = item.get("data", {}) or {}
        for k, raw in cm_data.items():
            if len(matches) >= limit:
                truncated = True
                break
            value = str(raw)
            # Match on key name (whole value excerpt) or per-line in the value.
            if q_low in (k or "").lower():
                matches.append({
                    "configmap": cm_name, "key": k, "match": "key",
                    "excerpt": _redact_cm_value(k, value.splitlines()[0] if value else "", _CM_EXCERPT_CAP),
                    "labels_hint": labels_hint,
                })
                continue
            for line_no, line in enumerate(value.splitlines(), start=1):
                if q_low in line.lower():
                    matches.append({
                        "configmap": cm_name, "key": k, "match": "value",
                        "line_no": line_no,
                        "excerpt": _redact_cm_value(k, line.strip(), _CM_EXCERPT_CAP),
                        "labels_hint": labels_hint,
                    })
                    break  # one excerpt per key is enough to locate it

    return {
        "namespace": namespace,
        "query": q,
        "searched_count": len(items),
        "match_count": len(matches),
        "matches": matches,
        "truncated": truncated,
    }


def list_services(namespace: str) -> Dict[str, Any]:
    """List all services in a namespace."""
    namespace = validate_namespace(namespace)
    data = get_runner().run_json(["get", "services", "-o", "json"], namespace=namespace)
    items = data.get("items", [])
    services = [parse_service(s) for s in items]
    return {"namespace": namespace, "service_count": len(services), "services": services}


def _parse_pod_text_output(text: str) -> List[Dict[str, Any]]:
    """Parse lightweight tabular ``kubectl get pods -A`` output.

    JSON output for all namespaces can exceed several MB on large clusters.
    The default table keeps all-namespace status checks fast and parseable.
    """
    lines = text.splitlines()
    if not lines:
        return []

    header = lines[0]
    known_cols = ["NAMESPACE", "NAME", "READY", "STATUS", "RESTARTS", "AGE"]
    col_starts: list[tuple[str, int]] = []
    search_from = 0
    for col in known_cols:
        idx = header.find(col, search_from)
        if idx >= 0:
            col_starts.append((col, idx))
            search_from = idx + len(col)

    if len(col_starts) < 3:
        logger.warning("Unexpected kubectl pod output format; cannot parse text output")
        return []

    pods: list[dict] = []
    for line in lines[1:]:
        if not line.strip():
            continue

        values: dict[str, str] = {}
        for i, (col_name, start) in enumerate(col_starts):
            end = col_starts[i + 1][1] if i + 1 < len(col_starts) else len(line)
            values[col_name] = line[start:end].strip() if start < len(line) else ""

        name = values.get("NAME", "")
        if not name:
            continue

        restarts = 0
        restarts_match = re.match(r"(\d+)", values.get("RESTARTS", ""))
        if restarts_match:
            restarts = int(restarts_match.group(1))

        status = values.get("STATUS", "Unknown")
        pods.append({
            "name": name,
            "namespace": values.get("NAMESPACE", ""),
            "phase": status if status in ("Running", "Pending", "Succeeded", "Failed", "Unknown") else "Running",
            "status": status,
            "status_reason": "",
            "ready": values.get("READY", "0/0"),
            "restarts": restarts,
            "restart_count": restarts,
            "node_name": "",
            "image": "",
            "images": [],
            "pod_ip": "",
            "creation_timestamp": "",
            "labels": {},
            "container_states": [],
            "conditions": {},
            "age": values.get("AGE", ""),
        })

    return pods


def get_pods(
    namespace: str,
    label_selector: Optional[str] = None,
    status_filter: Optional[str] = None,
    exclude_namespaces: Optional[List[str]] = None,
    exclude_namespace_prefixes: Optional[List[str]] = None,
    labels_only: bool = False,
    images_only: bool = False,
    resources_only: bool = False,
    placement_only: bool = False,
    details: bool = False,
) -> Dict[str, Any]:
    """
    List pods in namespace with optional label selector and status filter.

    Args:
        namespace: Namespace to query. Pass "*" or "all" to list pods across
                   all namespaces (equivalent to kubectl get pods -A).
        label_selector: Optional label selector (e.g., "app=myapp")
        status_filter: Optional status to filter results by (e.g., "CrashLoopBackOff")
        exclude_namespaces: Exact namespace names to exclude when querying all namespaces.
        exclude_namespace_prefixes: Namespace prefixes to exclude when querying all namespaces.
        labels_only/images_only/resources_only/placement_only: Focused output modes.
        details: Use JSON-backed safe pod summaries for broad all-namespace requests.

    Returns:
        Dict with pod summaries
    """
    all_namespaces = namespace in ("*", "all", "all-namespaces")
    focused_modes = {
        "labels": labels_only,
        "images": images_only,
        "resources": resources_only,
        "placement": placement_only,
    }
    active_modes = [name for name, enabled in focused_modes.items() if enabled]
    use_json_mode = bool(status_filter or details or active_modes)

    if not all_namespaces:
        namespace = validate_namespace(namespace)

    if label_selector:
        label_selector = validate_label_selector(label_selector)

    if all_namespaces:
        args = ["get", "pods", "--all-namespaces"]
        if label_selector:
            args.extend(["-l", label_selector])

        if use_json_mode:
            # Focused modes and status filters need JSON fields; the kubectl table
            # omits labels, images, resources, placement, and container state.
            # Query namespaces individually to avoid truncating large all-cluster
            # JSON output before it can be parsed.
            pods = []
            for ns in get_namespaces().get("namespaces", []):
                ns_args = ["get", "pods", "-o", "json"]
                if label_selector:
                    ns_args.extend(["-l", label_selector])
                result = get_runner().run_json(ns_args, namespace=ns["name"])
                pods.extend(parse_pod_list(result))
        else:
            result = get_runner().run(args)
            result.raise_for_status()
            pods = _parse_pod_text_output(result.stdout)
    else:
        args = ["get", "pods", "-o", "json"]
        if label_selector:
            args.extend(["-l", label_selector])
        result = get_runner().run_json(args, namespace=namespace)
        pods = parse_pod_list(result)

    def _build_health_summary(pod_list: list[dict]) -> dict:
        # Pre-compute health summary so AI synthesis sees problems even when
        # the full pod list is too large for the prompt window.
        unhealthy_statuses = {
            "CrashLoopBackOff", "Error", "OOMKilled", "ImagePullBackOff",
            "ErrImagePull", "CreateContainerError", "RunContainerError",
            "InvalidImageName", "ImageInspectError", "ErrImageNeverPull",
        }
        unhealthy = [p for p in pod_list if p.get("status") in unhealthy_statuses]
        high_restart = [p for p in pod_list if p.get("restarts", 0) >= 5 and p not in unhealthy]

        status_counts: dict = {}
        for p in pod_list:
            status = p.get("status", "Unknown")
            status_counts[status] = status_counts.get(status, 0) + 1

        return {
            "total": len(pod_list),
            "unhealthy_count": len(unhealthy),
            "unhealthy_pods": [
                {"name": p["name"], "status": p.get("status"), "restarts": p.get("restarts", 0)}
                for p in unhealthy
            ],
            "high_restart_pods": [
                {"name": p["name"], "status": p.get("status"), "restarts": p.get("restarts", 0)}
                for p in high_restart
            ],
            "status_breakdown": status_counts,
        }

    all_pods_health_summary = _build_health_summary(pods)

    if status_filter:
        lowered = status_filter.lower()
        pods = [
            p for p in pods
            if lowered in p.get("status", "").lower()
            or (p.get("status_reason") and lowered in p.get("status_reason", "").lower())
        ]

    excluded_names = set(exclude_namespaces or [])
    excluded_prefixes = tuple(exclude_namespace_prefixes or [])
    if all_namespaces and (excluded_names or excluded_prefixes):
        pods = [
            p for p in pods
            if p.get("namespace") not in excluded_names
            and not any((p.get("namespace") or "").startswith(prefix) for prefix in excluded_prefixes)
        ]

    health_summary = _build_health_summary(pods)
    namespace_summary: dict[str, int] = {}
    for pod in pods:
        ns = pod.get("namespace") or namespace
        namespace_summary[ns] = namespace_summary.get(ns, 0) + 1

    def _project_focused_pod(pod: dict) -> dict:
        base = {
            "namespace": pod.get("namespace"),
            "name": pod.get("name"),
        }
        if labels_only:
            return {
                **base,
                "labels": pod.get("labels", {}),
                "label_count": pod.get("label_count", len(pod.get("labels", {}) or {})),
            }
        if images_only:
            return {
                **base,
                "images": pod.get("images", []),
                "containers": [
                    {"name": c.get("name"), "image": c.get("image")}
                    for c in pod.get("containers", [])
                ],
                "init_containers": [
                    {"name": c.get("name"), "image": c.get("image")}
                    for c in pod.get("init_containers", [])
                ],
            }
        if resources_only:
            return {
                **base,
                "containers": [
                    {"name": c.get("name"), "resources": c.get("resources", {})}
                    for c in pod.get("containers", [])
                ],
                "init_containers": [
                    {"name": c.get("name"), "resources": c.get("resources", {})}
                    for c in pod.get("init_containers", [])
                ],
            }
        if placement_only:
            return {
                **base,
                "node_name": pod.get("node_name", ""),
                "pod_ip": pod.get("pod_ip", ""),
                "service_account_name": pod.get("service_account_name", ""),
                "node_selector": pod.get("node_selector", {}),
                "tolerations": pod.get("tolerations", []),
                "affinity": pod.get("affinity", {}),
                "owner_references": pod.get("owner_references", []),
            }
        return pod

    focused_pods = [_project_focused_pod(p) for p in pods] if active_modes else pods

    return {
        "namespace": "*" if all_namespaces else namespace,
        "label_selector": label_selector,
        "status_filter": status_filter,
        "exclude_namespaces": sorted(excluded_names) if excluded_names else None,
        "exclude_namespace_prefixes": list(excluded_prefixes) if excluded_prefixes else None,
        "labels_only": labels_only,
        "images_only": images_only,
        "resources_only": resources_only,
        "placement_only": placement_only,
        "details": details,
        "focused_modes": active_modes,
        "pod_count": len(pods),
        "namespace_summary": dict(sorted(namespace_summary.items())),
        "health_summary": health_summary,
        "all_pods_health_summary": all_pods_health_summary if status_filter else None,
        "pods": focused_pods,
    }


def describe_pod(namespace: str, pod_name: str) -> Dict[str, Any]:
    """
    Get detailed pod description with parsed highlights.
    
    Args:
        namespace: Namespace containing the pod
        pod_name: Name of the pod
        
    Returns:
        Dict with pod description and highlights
    """
    namespace = validate_namespace(namespace)
    pod_name = validate_resource_name(pod_name, "pod")
    
    # Get describe output
    result = get_runner().run(
        ["describe", "pod", pod_name],
        namespace=namespace
    )
    result.raise_for_status()
    
    # Parse highlights
    highlights = parse_pod_describe_highlights(result.stdout)
    
    response = {
        "namespace": namespace,
        "pod_name": pod_name,
        "highlights": highlights,
        "raw_output": result.stdout,
        "truncated": result.truncated,
    }

    if settings.enable_log_summarization and result.stdout:
        try:
            from services.summarizer import summarize_describe
            ds = summarize_describe(result.stdout)
            if ds.method != "none":
                response["describe_summary"] = ds.summary
                response["summary_method"] = ds.method
                response["summary_stats"] = {
                    "bytes_in": ds.stats.bytes_in,
                    "bytes_out": ds.stats.bytes_out,
                    "sections_kept": ds.stats.sections_kept,
                    "sections_dropped": ds.stats.sections_dropped,
                }
        except Exception as exc:
            logger.warning("Describe summarization failed: %s", exc)

    return response


def get_pod_logs(
    namespace: str,
    pod_name: str,
    previous: bool = False,
    tail: int = 200,
    container: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get pod logs with size limits.
    
    Args:
        namespace: Namespace containing the pod
        pod_name: Name of the pod
        previous: Get logs from previous container instance
        tail: Number of lines to retrieve (capped by settings)
        container: Optional container name for multi-container pods
        
    Returns:
        Dict with log content
    """
    namespace = validate_namespace(namespace)
    pod_name = validate_resource_name(pod_name, "pod")
    tail = validate_tail_lines(tail)
    
    args = ["logs", pod_name, f"--tail={tail}"]
    
    if previous:
        args.append("--previous")
    
    if container:
        # VALIDATION: Container names can have dots and underscores
        if not container or len(container) > 253:
            raise ValueError(f"Invalid container name: {container}")
        # Allow more characters for container names (dots, underscores)
        if not all(c.isalnum() or c in '-_.' for c in container):
            raise ValueError(
                f"Invalid container name: '{container}'. "
                "Container names must contain only alphanumeric characters, hyphens, dots, and underscores."
            )
        args.extend(["-c", container])
    
    try:
        result = get_runner().run(args, namespace=namespace)
        
        # Additional truncation if needed
        log_text, was_truncated = truncate_logs(
            result.stdout,
            settings.max_log_tail_lines
        )
        
        response = {
            "namespace": namespace,
            "pod_name": pod_name,
            "container": container,
            "previous": previous,
            "tail_lines": tail,
            "logs": log_text,
            "truncated": was_truncated or result.truncated,
            "success": True,
        }

        # Tool-result summarization (Phase 2.1). Raw `logs` stays canonical
        # for the UI; AI consumers should prefer `logs_summary` when present.
        if settings.enable_log_summarization and log_text:
            try:
                from services.summarizer import summarize_logs
                summary = summarize_logs(log_text)
                if summary.method != "none":
                    response["logs_summary"] = summary.summary
                    response["summary_method"] = summary.method
                    response["summary_stats"] = {
                        "bytes_in": summary.stats.bytes_in,
                        "bytes_out": summary.stats.bytes_out,
                        "lines_in": summary.stats.lines_in,
                        "lines_out": summary.stats.lines_out,
                        "duplicates_collapsed": summary.stats.duplicates_collapsed,
                        "error_lines": summary.stats.error_lines,
                        "warn_lines": summary.stats.warn_lines,
                    }
            except Exception as exc:
                logger.warning("Log summarization failed: %s", exc)

        return response

    except KubectlError as e:
        return {
            "namespace": namespace,
            "pod_name": pod_name,
            "container": container,
            "previous": previous,
            "tail_lines": tail,
            "logs": "",
            "error": str(e),
            "stderr": e.stderr,
            "success": False,
        }


def _classify_pod_failure_mode(pod_item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Classify pod state for automated investigation playbooks.

    Returns mode: Pending | ImagePullBackOff | CrashLoopBackOff | other

    Container waiting reasons are checked before phase=Pending so that
    ImagePullBackOff/CrashLoopBackOff on a not-yet-Running pod match the right playbook.
    """
    status = pod_item.get("status", {})
    phase = status.get("phase", "")

    image_reasons = ("ImagePullBackOff", "ErrImagePull", "InvalidImageName")
    crash_reasons = ("CrashLoopBackOff",)

    for cs in status.get("initContainerStatuses", []) or []:
        waiting = cs.get("state", {}).get("waiting", {})
        r = waiting.get("reason", "")
        if r in image_reasons:
            return {
                "mode": "ImagePullBackOff",
                "container": cs.get("name"),
                "reason": r,
                "message": waiting.get("message", ""),
            }
        if r in crash_reasons:
            return {
                "mode": "CrashLoopBackOff",
                "container": cs.get("name"),
                "reason": r,
                "message": waiting.get("message", ""),
            }

    for cs in status.get("containerStatuses", []) or []:
        waiting = cs.get("state", {}).get("waiting", {})
        r = waiting.get("reason", "")
        if r in image_reasons:
            return {
                "mode": "ImagePullBackOff",
                "container": cs.get("name"),
                "reason": r,
                "message": waiting.get("message", ""),
            }
        if r in crash_reasons:
            return {
                "mode": "CrashLoopBackOff",
                "container": cs.get("name"),
                "reason": r,
                "message": waiting.get("message", ""),
            }

    if phase == "Pending":
        return {
            "mode": "Pending",
            "container": None,
            "reason": status.get("reason", ""),
            "message": status.get("message", ""),
        }

    return {
        "mode": "other",
        "container": None,
        "reason": "",
        "message": "",
    }


def _container_status_findings(pod_json: Dict[str, Any]) -> list[dict]:
    """Return containers whose state/restarts deserve investigation."""
    findings: list[dict] = []
    for cs in pod_json.get("status", {}).get("containerStatuses", []) or []:
        name = cs.get("name")
        if not name:
            continue
        state = cs.get("state", {}) or {}
        waiting = state.get("waiting", {}) or {}
        terminated = state.get("terminated", {}) or {}
        running = state.get("running", {}) or {}
        last_terminated = (cs.get("lastState", {}) or {}).get("terminated", {}) or {}
        restart_count = cs.get("restartCount", 0) or 0
        ready = bool(cs.get("ready", False))

        reason = waiting.get("reason") or terminated.get("reason") or last_terminated.get("reason") or ""
        message = waiting.get("message") or terminated.get("message") or last_terminated.get("message") or ""
        should_include = bool(waiting or terminated or last_terminated or restart_count > 0 or not ready)
        if not should_include:
            continue

        findings.append({
            "container": name,
            "ready": ready,
            "restart_count": restart_count,
            "state": "Waiting" if waiting else "Terminated" if terminated else "Running" if running else "Unknown",
            "reason": reason,
            "message": message,
            "last_exit_code": last_terminated.get("exitCode"),
            "last_reason": last_terminated.get("reason"),
        })
    return findings


def _log_excerpt(log_result: Dict[str, Any], max_chars: int = 900) -> dict:
    text = str(log_result.get("logs_summary") or log_result.get("logs") or "")
    diagnostic = _application_dependency_resolution_issue(text)
    diagnostic_lines = diagnostic.get("evidence") if isinstance(diagnostic, dict) else None
    excerpt = "\n".join(diagnostic_lines)[:max_chars] if diagnostic_lines else text[:max_chars]
    return {
        "success": log_result.get("success", False),
        "excerpt": excerpt,
        "error": log_result.get("error") or log_result.get("stderr") or "",
        "truncated": bool(log_result.get("truncated") or len(text) > max_chars),
    }


def _application_dependency_resolution_issue(log_text: str) -> Optional[dict]:
    """Extract explicit application dependency prerequisite/version conflicts."""
    if not log_text:
        return None
    lowered = log_text.lower()
    if (
        "older version defined on the top level" not in lowered
        and "plugin prerequisites not met" not in lowered
    ):
        return None

    mismatches: list[dict] = []
    pattern = re.compile(
        r"depends on\s+([^,\n]+),\s*but there is an older version defined on the top level\s*-\s*([^\n]+)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(log_text):
        required = match.group(1).strip().rstrip(".")
        pinned = match.group(2).strip().rstrip(".")
        if required and pinned:
            mismatches.append({
                "required": required,
                "pinned": pinned,
            })
        if len(mismatches) >= 5:
            break

    evidence_lines = [
        line.strip()
        for line in log_text.splitlines()
        if (
            "plugin prerequisites not met" in line.lower()
            or "older version defined on the top level" in line.lower()
        )
    ][:8]

    return {
        "type": "application_dependency_resolution",
        "mismatches": mismatches,
        "evidence": evidence_lines,
    }


def _collect_container_log_findings(
    namespace: str,
    pod_name: str,
    tail: int,
    pod_json: Dict[str, Any],
    primary_container: Optional[str],
    primary_current: Optional[Dict[str, Any]] = None,
    primary_previous: Optional[Dict[str, Any]] = None,
) -> list[dict]:
    """Collect compact current/previous logs for every suspect container."""
    findings = _container_status_findings(pod_json)
    if primary_container and not any(f.get("container") == primary_container for f in findings):
        findings.insert(0, {
            "container": primary_container,
            "ready": None,
            "restart_count": None,
            "state": "Selected",
            "reason": "selected_by_classification",
            "message": "",
            "last_exit_code": None,
            "last_reason": None,
        })

    log_tail = min(tail, 120)
    for finding in findings:
        container = finding.get("container")
        if not container:
            continue
        if container == primary_container and isinstance(primary_current, dict):
            current = primary_current
        else:
            current = get_pod_logs(namespace, pod_name, previous=False, tail=log_tail, container=container)
        if container == primary_container and isinstance(primary_previous, dict):
            previous = primary_previous
        else:
            previous = get_pod_logs(namespace, pod_name, previous=True, tail=log_tail, container=container)

        finding["logs_current"] = _log_excerpt(current)
        finding["logs_previous"] = _log_excerpt(previous)
        combined_logs = "\n".join([
            str(current.get("logs") or current.get("logs_summary") or ""),
            str(previous.get("logs") or previous.get("logs_summary") or ""),
        ])
        dependency_issue = _application_dependency_resolution_issue(combined_logs)
        if dependency_issue:
            finding["diagnostic_issue"] = dependency_issue

    return findings


def _pod_events_field_selector(pod_name: str) -> str:
    """Field selector for events involving this Pod (read-only)."""
    return f"involvedObject.name={pod_name},involvedObject.kind=Pod"


def _container_env_map(pod_json: Dict[str, Any], container_name: Optional[str]) -> Dict[str, str]:
    """Return literal env vars for a container from the pod spec."""
    if not container_name:
        return {}

    for container in pod_json.get("spec", {}).get("containers", []) or []:
        if container.get("name") != container_name:
            continue
        env: Dict[str, str] = {}
        for item in container.get("env", []) or []:
            if "value" in item:
                env[item.get("name", "")] = item.get("value", "")
        return env
    return {}


def _all_container_env_maps(pod_json: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """Return literal env vars keyed by container name."""
    env_by_container: Dict[str, Dict[str, str]] = {}
    for container in pod_json.get("spec", {}).get("containers", []) or []:
        name = container.get("name", "")
        if name:
            env_by_container[name] = _container_env_map(pod_json, name)
    return env_by_container


def _service_dependency_check(namespace: str, service_name: str) -> Dict[str, Any]:
    """Check whether a same-namespace service and endpoints exist."""
    check: Dict[str, Any] = {
        "service": service_name,
        "namespace": namespace,
        "service_exists": False,
        "endpoints_exist": False,
        "ready_addresses": 0,
    }

    try:
        get_runner().run_json(["get", "service", service_name, "-o", "json"], namespace=namespace)
        check["service_exists"] = True
    except Exception as exc:
        check["service_error"] = str(exc)

    try:
        endpoints = get_runner().run_json(["get", "endpoints", service_name, "-o", "json"], namespace=namespace)
        subsets = endpoints.get("subsets", []) or []
        check["endpoints_exist"] = bool(subsets)
        check["ready_addresses"] = sum(len(s.get("addresses", []) or []) for s in subsets)
    except Exception as exc:
        check["endpoints_error"] = str(exc)

    return check


def _build_investigation_evidence(
    namespace: str,
    pod_json: Dict[str, Any],
    log_container: Optional[str],
    logs: str,
    container_log_findings: Optional[list[dict]] = None,
) -> Dict[str, Any]:
    """Build deterministic evidence so AI summaries do not overfit weak clues."""
    env_by_container = _all_container_env_maps(pod_json)
    env = _container_env_map(pod_json, log_container)
    evidence: list[Any] = []
    dependency_checks: list[dict] = []
    secondary_issues: list[dict] = []
    suspected_root_cause = ""
    suggested_fix = ""

    zookeeper_sources = [
        (container, values["KAFKA_ZOOKEEPER_CONNECT"])
        for container, values in env_by_container.items()
        if values.get("KAFKA_ZOOKEEPER_CONNECT")
    ]
    if not zookeeper_sources and env.get("KAFKA_ZOOKEEPER_CONNECT"):
        zookeeper_sources = [(log_container or "unknown", env["KAFKA_ZOOKEEPER_CONNECT"])]

    for container_name, zookeeper_connect in zookeeper_sources:
        evidence.append(f"{container_name}: KAFKA_ZOOKEEPER_CONNECT={zookeeper_connect}")
        for endpoint in zookeeper_connect.split(","):
            host_port = endpoint.strip()
            if not host_port:
                continue
            host = host_port.rsplit(":", 1)[0]
            if "." in host:
                dependency_checks.append({
                    "type": "zookeeper",
                    "target": host_port,
                    "checked": False,
                    "reason": "FQDN or external host; skipped same-namespace service check",
                })
                continue

            check = _service_dependency_check(namespace, host)
            check["type"] = "zookeeper"
            check["target"] = host_port
            dependency_checks.append(check)
            if not check.get("service_exists"):
                suspected_root_cause = (
                    f"Kafka is configured to use ZooKeeper service `{host}`, but that "
                    f"service does not exist in namespace `{namespace}`."
                )
                suggested_fix = (
                    "Restore the missing ZooKeeper service/backing pods, or update "
                    "KAFKA_ZOOKEEPER_CONNECT to the correct ZooKeeper service DNS name."
                )
            elif not check.get("endpoints_exist") or check.get("ready_addresses", 0) == 0:
                suspected_root_cause = (
                    f"Kafka is configured to use ZooKeeper service `{host}`, but the "
                    "service has no ready endpoints."
                )
                suggested_fix = (
                    "Fix the ZooKeeper pods/selectors so the service has ready endpoints, "
                    "then restart the Kafka StatefulSet."
                )

    if "Check if Zookeeper is healthy" in logs:
        evidence.append("Kafka exits during the Confluent preflight step: Check if Zookeeper is healthy")

    dependency_issue = _application_dependency_resolution_issue(logs)
    if dependency_issue:
        evidence.append(dependency_issue)
        first_mismatch = (dependency_issue.get("mismatches") or [{}])[0]
        required = first_mismatch.get("required")
        pinned = first_mismatch.get("pinned")
        if required and pinned:
            suspected_root_cause = (
                "The failing container is exiting during application dependency resolution: "
                f"a dependency requires `{required}`, but the chart/config pins an older top-level dependency `{pinned}`."
            )
        else:
            suspected_root_cause = (
                "The failing container is exiting during application dependency resolution because "
                "one or more top-level dependency pins do not satisfy required prerequisites."
            )
        suggested_fix = (
            "Update the application/plugin dependency pin list or Helm values so top-level versions "
            "satisfy the required dependencies, then redeploy the workload."
        )

    for finding in container_log_findings or []:
        container = finding.get("container")
        if not container:
            continue
        reason = finding.get("reason") or finding.get("last_reason") or ""
        restart_count = finding.get("restart_count")
        previous_excerpt = ((finding.get("logs_previous") or {}).get("excerpt") or "")
        current_excerpt = ((finding.get("logs_current") or {}).get("excerpt") or "")
        excerpt = previous_excerpt or current_excerpt
        if reason or restart_count:
            evidence.append({
                "type": "container_state",
                "container": container,
                "reason": reason,
                "restart_count": restart_count,
            })
        lowered = excerpt.lower()
        diagnostic_issue = finding.get("diagnostic_issue") if isinstance(finding.get("diagnostic_issue"), dict) else None
        if diagnostic_issue and diagnostic_issue.get("type") == "application_dependency_resolution":
            evidence.append({
                "type": "container_log_diagnostic",
                "container": container,
                "diagnostic": diagnostic_issue,
            })
            first_mismatch = (diagnostic_issue.get("mismatches") or [{}])[0]
            required = first_mismatch.get("required")
            pinned = first_mismatch.get("pinned")
            if required and pinned:
                suspected_root_cause = (
                    f"Container `{container}` is exiting during application dependency resolution: "
                    f"a dependency requires `{required}`, but the chart/config pins an older top-level dependency `{pinned}`."
                )
            else:
                suspected_root_cause = (
                    f"Container `{container}` is exiting during application dependency resolution because "
                    "one or more top-level dependency pins do not satisfy required prerequisites."
                )
            suggested_fix = (
                "Update the application/plugin dependency pin list or Helm values so top-level versions "
                "satisfy the required dependencies, then redeploy the workload."
            )
        if (
            "prometheus" in container.lower()
            or "jmx" in container.lower()
            or "jmx" in lowered
            or "javaagent" in lowered
            or "unable to access jarfile" in lowered
        ):
            secondary_issues.append({
                "container": container,
                "reason": reason or "log evidence",
                "evidence": excerpt[:500],
            })

    return {
        "suspected_root_cause": suspected_root_cause,
        "suggested_fix": suggested_fix,
        "evidence": evidence,
        "dependency_checks": dependency_checks,
        "secondary_issues": secondary_issues,
    }


def _safe_pod_spec_summary(pod_json: Dict[str, Any]) -> Dict[str, Any]:
    parsed = parse_pod_list({"items": [pod_json]})
    if not parsed:
        return {}
    pod = parsed[0]
    return {
        "name": pod.get("name", ""),
        "namespace": pod.get("namespace", ""),
        "labels": pod.get("labels", {}),
        "label_count": pod.get("label_count", 0),
        "owner_references": pod.get("owner_references", []),
        "service_account_name": pod.get("service_account_name", ""),
        "node_name": pod.get("node_name", ""),
        "node_selector": pod.get("node_selector", {}),
        "tolerations": pod.get("tolerations", []),
        "affinity": pod.get("affinity", {}),
        "images": pod.get("images", []),
        "containers": pod.get("containers", []),
        "init_containers": pod.get("init_containers", []),
        "volumes": pod.get("volumes", []),
    }


def _safe_pod_template_summary_from_workload(wl_json: Dict[str, Any]) -> Dict[str, Any]:
    metadata = wl_json.get("metadata", {}) or {}
    template = wl_json.get("spec", {}).get("template", {}) or {}
    template_meta = template.get("metadata", {}) or {}
    template_spec = template.get("spec", {}) or {}
    pseudo_pod = {
        "metadata": {
            "name": metadata.get("name", ""),
            "namespace": metadata.get("namespace", ""),
            "labels": template_meta.get("labels", {}) or {},
            "ownerReferences": [],
        },
        "spec": template_spec,
        "status": {},
    }
    return _safe_pod_spec_summary(pseudo_pod)


def _workload_summary(wl_json: Dict[str, Any], workload_type: str) -> Dict[str, Any]:
    metadata = wl_json.get("metadata", {}) or {}
    spec = wl_json.get("spec", {}) or {}
    status = wl_json.get("status", {}) or {}
    labels = metadata.get("labels", {}) or {}
    annotations = metadata.get("annotations", {}) or {}

    summary: Dict[str, Any] = {
        "name": metadata.get("name", ""),
        "namespace": metadata.get("namespace", ""),
        "kind": wl_json.get("kind", workload_type),
        "type": workload_type,
        "labels": labels,
        "label_count": len(labels),
        "annotations": _metadata_annotations_summary(metadata),
        "selector": spec.get("selector", {}) or {},
        "generation": metadata.get("generation"),
        "observed_generation": status.get("observedGeneration"),
        "creation_timestamp": metadata.get("creationTimestamp", ""),
        "pod_template": _safe_pod_template_summary_from_workload(wl_json),
    }

    if workload_type == "daemonset":
        desired = status.get("desiredNumberScheduled", 0)
        ready = status.get("numberReady", 0)
        summary["replicas"] = {
            "desired": desired,
            "ready": ready,
            "available": status.get("numberAvailable", 0),
            "updated": status.get("updatedNumberScheduled", 0),
            "unavailable": status.get("numberUnavailable", 0),
        }
        summary["health_status"] = "healthy" if ready == desired and desired > 0 else "unhealthy"
    else:
        desired = spec.get("replicas", 0)
        ready = status.get("readyReplicas", 0)
        summary["replicas"] = {
            "desired": desired,
            "current": status.get("replicas", 0),
            "updated": status.get("updatedReplicas", 0),
            "ready": ready,
            "available": status.get("availableReplicas", 0),
            "unavailable": status.get("unavailableReplicas", 0),
        }
        summary["health_status"] = "healthy" if ready == desired and desired > 0 else "unhealthy"

    summary["conditions"] = [
        {
            "type": cond.get("type", ""),
            "status": cond.get("status", ""),
            "reason": cond.get("reason", ""),
            "message": cond.get("message", ""),
            "last_update_time": cond.get("lastUpdateTime", ""),
            "last_transition_time": cond.get("lastTransitionTime", ""),
        }
        for cond in status.get("conditions", []) or []
        if isinstance(cond, dict)
    ]
    if annotations.get("deployment.kubernetes.io/revision"):
        summary["revision"] = annotations.get("deployment.kubernetes.io/revision", "")
    return summary


def _selector_match_labels(selector: dict) -> dict:
    if not isinstance(selector, dict):
        return {}
    match_labels = selector.get("matchLabels")
    if isinstance(match_labels, dict):
        return match_labels
    return {
        key: value
        for key, value in selector.items()
        if key != "matchExpressions" and isinstance(value, str)
    }


def investigate_pod(
    namespace: str,
    pod_name: str,
    tail: int = 200,
    use_ai: bool = True,
) -> Dict[str, Any]:
    """
    Run a read-only investigation playbook for one pod based on failure mode.

    Branches (automatic):
    - CrashLoopBackOff: describe → logs (current) → logs (previous) → events
    - ImagePullBackOff: describe → events
    - Pending: describe → events
    - other: describe → events (minimal)

    After gathering kubectl data, optionally calls Gemini AI for diagnosis and fix commands.

    Args:
        namespace: Namespace containing the pod
        pod_name: Pod name
        tail: Log tail lines (capped by settings)
        use_ai: Call Gemini AI for diagnosis after gathering kubectl data (default: True)

    Returns:
        Aggregated dict with classification, step outputs, and optional AI analysis
    """
    namespace = validate_namespace(namespace)
    pod_name = validate_resource_name(pod_name, "pod")
    tail = validate_tail_lines(tail)

    try:
        pod_json = get_runner().run_json(
            ["get", "pod", pod_name, "-o", "json"],
            namespace=namespace
        )
    except KubectlError as e:
        return {
            "success": False,
            "error": str(e),
            "stderr": e.stderr,
            "namespace": namespace,
            "pod_name": pod_name,
        }

    classification = _classify_pod_failure_mode(pod_json)
    mode = classification["mode"]

    spec_containers = pod_json.get("spec", {}).get("containers", [])
    default_container = spec_containers[0].get("name") if spec_containers else None
    log_container = classification.get("container") or default_container

    result: Dict[str, Any] = {
        "success": True,
        "namespace": namespace,
        "pod_name": pod_name,
        "classification": classification,
        "playbook": mode,
        "pod_spec_summary": _safe_pod_spec_summary(pod_json),
        "steps_run": [],
    }

    describe = describe_pod(namespace, pod_name)
    result["describe"] = describe
    result["steps_run"].append("describe_pod")

    event_fs = _pod_events_field_selector(pod_name)

    if mode == "CrashLoopBackOff":
        result["logs_current"] = get_pod_logs(
            namespace, pod_name, previous=False, tail=tail, container=log_container
        )
        result["steps_run"].append("get_pod_logs")
        result["logs_previous"] = get_pod_logs(
            namespace, pod_name, previous=True, tail=tail, container=log_container
        )
        result["steps_run"].append("get_pod_logs_previous")
        result["events"] = get_events(namespace, field_selector=event_fs)
        result["steps_run"].append("get_events")
        result["container_log_findings"] = _collect_container_log_findings(
            namespace,
            pod_name,
            tail,
            pod_json,
            log_container,
            result.get("logs_current") if isinstance(result.get("logs_current"), dict) else None,
            result.get("logs_previous") if isinstance(result.get("logs_previous"), dict) else None,
        )
        if result["container_log_findings"]:
            result["steps_run"].append("collect_container_log_findings")
    elif mode == "ImagePullBackOff":
        result["events"] = get_events(namespace, field_selector=event_fs)
        result["steps_run"].append("get_events")
    elif mode == "Pending":
        result["events"] = get_events(namespace, field_selector=event_fs)
        result["steps_run"].append("get_events")
    else:
        result["note"] = (
            "Pod did not match Pending, ImagePullBackOff, or CrashLoopBackOff. "
            "Included describe and filtered events only."
        )
        result["events"] = get_events(namespace, field_selector=event_fs)
        result["steps_run"].append("get_events")

    logs_for_evidence_parts: list[str] = []
    if isinstance(result.get("logs_current"), dict):
        logs_for_evidence_parts.append(str(result["logs_current"].get("logs", "")))
    if isinstance(result.get("logs_previous"), dict):
        logs_for_evidence_parts.append(str(result["logs_previous"].get("logs", "")))
    logs_for_evidence = "\n".join(part for part in logs_for_evidence_parts if part)
    evidence_summary = _build_investigation_evidence(
        namespace,
        pod_json,
        log_container,
        logs_for_evidence,
        result.get("container_log_findings") if isinstance(result.get("container_log_findings"), list) else None,
    )
    if evidence_summary.get("evidence") or evidence_summary.get("dependency_checks"):
        result["evidence_summary"] = evidence_summary

    # ── Gemini AI analysis (optional) ──────────────────────────────────────────
    if use_ai and _ai_service_available and _llm_service:
        try:
            ai_result = _llm_service.analyze_live_investigation(pod_name, namespace, result)
            result["ai"] = ai_result
            result["steps_run"].append("ai_analysis")
        except Exception as e:
            logger.warning(f"AI analysis failed (non-fatal): {e}")
            result["ai"] = {"ai_enabled": False, "error": str(e)}
    elif use_ai:
        result["ai"] = {"ai_enabled": False, "message": "AI service not available"}

    return result


def get_events(namespace: str, field_selector: Optional[str] = None) -> Dict[str, Any]:
    """
    Get recent events in a namespace, or across all namespaces.

    Args:
        namespace: Namespace to query. Pass "*" or "all" to search all namespaces
                   (equivalent to kubectl get events -A).
        field_selector: Optional field selector for filtering (e.g. "type=Warning")

    Returns:
        Dict with parsed events (limited to most recent 50)
    """
    all_namespaces = namespace in ("*", "all", "all-namespaces")

    if not all_namespaces:
        namespace = validate_namespace(namespace)

    args = ["get", "events", "-o", "json"]

    if all_namespaces:
        args.append("--all-namespaces")

    if field_selector:
        if not all(c.isalnum() or c in '-_.,=!' for c in field_selector):
            raise ValueError(
                f"Invalid field selector: '{field_selector}'. "
                "Field selectors must contain only alphanumeric characters and -_.=!,"
            )
        if any(dangerous in field_selector for dangerous in [";", "&", "|", "`", "$", "(", ")"]):
            raise ValueError("Field selector contains forbidden characters")
        args.extend(["--field-selector", field_selector])

    try:
        # When using --all-namespaces don't pass a -n flag
        result = get_runner().run_json(args, namespace=None if all_namespaces else namespace)
        events = parse_events(result)
    except Exception as e:
        logger.error(f"Failed to get events: {e}")
        return {
            "namespace": "*" if all_namespaces else namespace,
            "event_count": 0,
            "events": [],
            "error": str(e),
            "truncated": False,
        }

    # SAFETY: Limit number of events returned to prevent huge outputs
    max_events = 50
    original_count = len(events)
    if len(events) > max_events:
        events = events[:max_events]
        truncated = True
    else:
        truncated = False

    response = {
        "namespace": "*" if all_namespaces else namespace,
        "event_count": len(events),
        "original_count": original_count,
        "events": events,
        "truncated": truncated,
    }

    if settings.enable_log_summarization and events:
        try:
            from services.summarizer import summarize_events
            es = summarize_events(events)
            if es.method != "none":
                response["events_summary"] = es.summary
                response["summary_method"] = es.method
                response["summary_stats"] = {
                    "events_in": es.stats.events_in,
                    "events_out": es.stats.events_out,
                    "noise_dropped": es.stats.noise_dropped,
                    "clusters": es.stats.clusters,
                }
        except Exception as exc:
            logger.warning("Events summarization failed: %s", exc)

    return response


def get_deployment(
    namespace: str,
    deployment_name: str,
    labels_only: bool = False,
    images_only: bool = False,
    resources_only: bool = False,
    template_only: bool = False,
) -> Dict[str, Any]:
    """
    Get deployment status and details.
    
    Args:
        namespace: Namespace containing the deployment
        deployment_name: Name of the deployment
        labels_only/images_only/resources_only/template_only: Focused output modes.
        
    Returns:
        Dict with deployment details
    """
    namespace = validate_namespace(namespace)
    deployment_name = validate_resource_name(deployment_name, "deployment")
    
    result = get_runner().run_json(
        ["get", "deployment", deployment_name, "-o", "json"],
        namespace=namespace
    )
    
    deployment = parse_deployment(result)
    focused_modes = {
        "labels": labels_only,
        "images": images_only,
        "resources": resources_only,
        "template": template_only,
    }
    active_modes = [name for name, enabled in focused_modes.items() if enabled]
    deployment["focused_modes"] = active_modes

    if labels_only:
        return {
            "name": deployment.get("name", deployment_name),
            "namespace": deployment.get("namespace", namespace),
            "focused_modes": active_modes,
            "labels": deployment.get("labels", {}),
            "label_count": deployment.get("label_count", 0),
            "selector": deployment.get("selector", {}),
            "pod_template": {
                "labels": deployment.get("pod_template", {}).get("labels", {}),
                "label_count": deployment.get("pod_template", {}).get("label_count", 0),
            },
        }

    if images_only:
        template = deployment.get("pod_template", {})
        return {
            "name": deployment.get("name", deployment_name),
            "namespace": deployment.get("namespace", namespace),
            "focused_modes": active_modes,
            "images": template.get("images", []),
            "containers": [
                {"name": c.get("name"), "image": c.get("image")}
                for c in template.get("containers", [])
            ],
            "init_containers": [
                {"name": c.get("name"), "image": c.get("image")}
                for c in template.get("init_containers", [])
            ],
        }

    if resources_only:
        template = deployment.get("pod_template", {})
        return {
            "name": deployment.get("name", deployment_name),
            "namespace": deployment.get("namespace", namespace),
            "focused_modes": active_modes,
            "containers": [
                {"name": c.get("name"), "resources": c.get("resources", {})}
                for c in template.get("containers", [])
            ],
            "init_containers": [
                {"name": c.get("name"), "resources": c.get("resources", {})}
                for c in template.get("init_containers", [])
            ],
        }

    if template_only:
        return {
            "name": deployment.get("name", deployment_name),
            "namespace": deployment.get("namespace", namespace),
            "focused_modes": active_modes,
            "selector": deployment.get("selector", {}),
            "pod_template": deployment.get("pod_template", {}),
        }
    
    return deployment


def get_service(
    namespace: str,
    service_name: str,
    ports_only: bool = False,
    selector_only: bool = False,
    traffic_policy_only: bool = False,
) -> Dict[str, Any]:
    """
    Get service details.
    
    Args:
        namespace: Namespace containing the service
        service_name: Name of the service
        ports_only/selector_only/traffic_policy_only: Focused output modes.
        
    Returns:
        Dict with service details
    """
    namespace = validate_namespace(namespace)
    service_name = validate_resource_name(service_name, "service")
    
    result = get_runner().run_json(
        ["get", "service", service_name, "-o", "json"],
        namespace=namespace
    )
    
    service = parse_service(result)
    focused_modes = {
        "ports": ports_only,
        "selector": selector_only,
        "traffic_policy": traffic_policy_only,
    }
    active_modes = [name for name, enabled in focused_modes.items() if enabled]
    service["focused_modes"] = active_modes

    base = {
        "name": service.get("name", service_name),
        "namespace": service.get("namespace", namespace),
        "type": service.get("type", ""),
        "focused_modes": active_modes,
    }
    if ports_only:
        return {**base, "ports": service.get("ports", [])}
    if selector_only:
        return {
            **base,
            "selector": service.get("selector", {}),
            "labels": service.get("labels", {}),
            "label_count": service.get("label_count", 0),
            "diagnostic_hint": service.get("diagnostic_hint"),
        }
    if traffic_policy_only:
        return {
            **base,
            "external_traffic_policy": service.get("external_traffic_policy", ""),
            "internal_traffic_policy": service.get("internal_traffic_policy", ""),
            "session_affinity": service.get("session_affinity", ""),
            "ip_families": service.get("ip_families", []),
            "ip_family_policy": service.get("ip_family_policy", ""),
            "external_ips": service.get("external_ips", []),
            "load_balancer": service.get("load_balancer", {}),
        }
    
    return service


def get_endpoints(namespace: str, service_name: str, include_slices: bool = True) -> Dict[str, Any]:
    """
    Get service endpoints to check if pods are backing the service.
    
    Args:
        namespace: Namespace containing the service
        service_name: Name of the service
        include_slices: Include EndpointSlice-backed readiness details when available.
        
    Returns:
        Dict with endpoint details
    """
    namespace = validate_namespace(namespace)
    service_name = validate_resource_name(service_name, "service")
    
    result = get_runner().run_json(
        ["get", "endpoints", service_name, "-o", "json"],
        namespace=namespace
    )
    
    endpoints = parse_endpoints(result)
    endpoints["include_slices"] = include_slices

    if include_slices:
        try:
            slice_result = get_runner().run_json(
                [
                    "get",
                    "endpointslices",
                    "-l",
                    f"kubernetes.io/service-name={service_name}",
                    "-o",
                    "json",
                ],
                namespace=namespace
            )
            endpoint_slices = parse_endpoint_slices(slice_result)
            endpoints["endpoint_slices"] = endpoint_slices
            endpoints["endpoint_slice_count"] = endpoint_slices.get("slice_count", 0)
            endpoints["endpoint_slice_endpoint_count"] = endpoint_slices.get("endpoint_count", 0)
            if endpoint_slices.get("endpoint_count", 0) > 0:
                endpoints["has_endpoints"] = endpoint_slices.get("ready_count", 0) > 0
            if endpoint_slices.get("diagnostic_hint"):
                endpoints["endpoint_slice_diagnostic_hint"] = endpoint_slices["diagnostic_hint"]
                if not endpoints.get("diagnostic_hint") or endpoint_slices.get("ready_count", 0) == 0:
                    endpoints["diagnostic_hint"] = endpoint_slices["diagnostic_hint"]
        except Exception as exc:
            endpoints["endpoint_slices"] = {
                "slice_count": 0,
                "endpoint_count": 0,
                "endpoints": [],
                "slices": [],
                "ports": [],
                "error": str(exc),
            }
    
    return endpoints


def get_rollout_status(namespace: str, deployment_name: str) -> Dict[str, Any]:
    """
    Get rollout status for deployment.
    
    Args:
        namespace: Namespace containing the deployment
        deployment_name: Name of the deployment
        
    Returns:
        Dict with rollout status
    """
    namespace = validate_namespace(namespace)
    deployment_name = validate_resource_name(deployment_name, "deployment")
    
    result = get_runner().run(
        ["rollout", "status", f"deployment/{deployment_name}"],
        namespace=namespace
    )
    
    return {
        "namespace": namespace,
        "deployment_name": deployment_name,
        "status": result.stdout.strip(),
        "success": result.success,
    }


def k8sgpt_analyze(
    namespace: Optional[str] = None,
    filter_text: Optional[str] = None
) -> Dict[str, Any]:
    """
    Run k8sgpt analysis if available.
    
    Args:
        namespace: Optional namespace to analyze
        filter_text: Optional filter for analysis
        
    Returns:
        Dict with k8sgpt output or error message
    """
    if not settings.enable_k8sgpt:
        return {
            "enabled": False,
            "message": "k8sgpt is not enabled. Set ENABLE_K8SGPT=true in .env",
        }
    
    if namespace:
        namespace = validate_namespace(namespace)
    
    # Build k8sgpt command - use array for safety
    cmd = ["k8sgpt", "analyze", "--output", "json"]
    
    if namespace:
        cmd.extend(["--namespace", namespace])
    
    if filter_text:
        # SECURITY: Strict validation for filter text
        if not filter_text.replace("-", "").replace("_", "").isalnum():
            raise ValueError(
                f"Invalid filter text: '{filter_text}'. "
                "Filter must contain only alphanumeric characters, hyphens, and underscores."
            )
        cmd.extend(["--filter", filter_text])
    
    try:
        import json
        import subprocess

        # NOTE: k8sgpt is intentionally kept off the reliability-critical path.
        # The request-scoped SSH/context-aware execution below was prototyped,
        # but is disabled until this optional tool has dedicated validation.
        #
        # runner = get_runner()
        # is_ssh = type(runner).__name__ == "SSHKubectlRunner"
        # if is_ssh:
        #     if hasattr(runner, "context") and runner.context:
        #         cmd.extend(["--kubecontext", runner.context])
        #     logger.info("SSH exec for k8sgpt on %s: %s", runner.host, " ".join(cmd))
        #     stdout, stderr, returncode = runner.run_shell_command(
        #         cmd, timeout=settings.kubectl_timeout_seconds
        #     )
        # else:
        #     if hasattr(runner, "kubeconfig_path") and runner.kubeconfig_path:
        #         cmd.extend(["--kubeconfig", str(runner.kubeconfig_path)])
        #     if hasattr(runner, "context") and runner.context:
        #         cmd.extend(["--kubecontext", runner.context])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=settings.kubectl_timeout_seconds,
            check=False,
            shell=False
        )
        stdout = result.stdout
        stderr = result.stderr
        returncode = result.returncode

        # SAFETY: Truncate output if too large
        truncated = False

        if len(stdout) > settings.max_output_bytes:
            stdout = stdout[:settings.max_output_bytes]
            truncated = True
        
        if len(stderr) > settings.max_output_bytes:
            stderr = stderr[:settings.max_output_bytes]
        
        if returncode == 0:
            try:
                analysis = json.loads(stdout)
                return {
                    "enabled": True,
                    "success": True,
                    "namespace": namespace,
                    "analysis": analysis,
                    "truncated": truncated,
                }
            except json.JSONDecodeError as e:
                return {
                    "enabled": True,
                    "success": False,
                    "error": f"Failed to parse k8sgpt output: {e}",
                    "raw_output": stdout[:1000] if stdout else "",
                }
        else:
            return {
                "enabled": True,
                "success": False,
                "error": stderr or "k8sgpt command failed",
            }
            
    except FileNotFoundError:
        return {
            "enabled": True,
            "success": False,
            "error": "k8sgpt CLI not found. Please install k8sgpt: https://k8sgpt.ai/",
        }
    except subprocess.TimeoutExpired:
        return {
            "enabled": True,
            "success": False,
            "error": f"k8sgpt command timed out after {settings.kubectl_timeout_seconds}s",
        }
    except Exception as e:
        logger.exception("Unexpected error running k8sgpt")
        return {
            "enabled": True,
            "success": False,
            "error": f"Unexpected error: {str(e)}",
        }


def add_kubeconfig_context(
    ssh_connection: str,
    password: Optional[str] = None,
    context_name: Optional[str] = None,
    port: int = 22
) -> Dict[str, Any]:
    """
    Add a new kubeconfig context via SSH.
    
    This function connects to a remote Kubernetes master node via SSH
    and adds its kubeconfig to the local configuration.
    
    Supports both key-based and password-based authentication.
    
    Args:
        ssh_connection: SSH connection string (e.g., 'user@hostname')
        password: Optional SSH password (if not using key-based auth)
        context_name: Optional custom context name (defaults to hostname)
        port: SSH port (default: 22)
        
    Returns:
        Dict with operation result
    """
    # Validate SSH connection format
    if not re.match(r'^[\w\-\.]+@[\w\-\.]+$', ssh_connection):
        return {
            "success": False,
            "error": "Invalid SSH connection format. Expected: user@hostname"
        }
    
    # Extract user and hostname
    username, hostname = ssh_connection.split('@')
    
    if not context_name:
        context_name = hostname.split('.')[0]
    
    # Validate context name
    if not re.match(r'^[\w\-]+$', context_name):
        return {
            "success": False,
            "error": "Invalid context name. Must contain only alphanumeric characters and hyphens."
        }
    
    try:
        import paramiko
        from io import StringIO
        
        logger.info(f"Connecting to {hostname}:{port} as {username}")

        # Create SSH client
        ssh = paramiko.SSHClient()
        # Fail closed on an unrecognised host key. This used AutoAddPolicy,
        # which trusts whatever key the far end presents — on the one path that
        # then sends the operator's SSH password to a control-plane node. Shared
        # with SSHKubectlRunner so the decision cannot drift apart again.
        from k8s.ssh_runner import HostKeyUnavailable, harden_host_keys

        try:
            harden_host_keys(ssh)
        except HostKeyUnavailable as exc:
            return {
                "success": False,
                "error": (
                    f"Refusing to connect to {hostname}: {exc}"
                ),
                "remediation": (
                    f"Register the host key first, after verifying the fingerprint "
                    f"out of band:\n  ssh-keyscan -p {port} {hostname} >> ~/.ssh/known_hosts"
                ),
            }

        # Connect with password or key-based auth
        try:
            if password:
                # Password-based authentication
                logger.info("Using password authentication")
                ssh.connect(
                    hostname=hostname,
                    port=port,
                    username=username,
                    password=password,
                    timeout=30,
                    look_for_keys=False,
                    allow_agent=False
                )
            else:
                # Key-based authentication (default)
                logger.info("Using key-based authentication")
                ssh.connect(
                    hostname=hostname,
                    port=port,
                    username=username,
                    timeout=30
                )
        except paramiko.AuthenticationException:
            return {
                "success": False,
                "error": "Authentication failed. Check username/password or SSH keys.",
                "ssh_connection": ssh_connection
            }
        except paramiko.SSHException as e:
            # RejectPolicy surfaces an unknown host as a generic SSHException.
            # Say what actually happened and what to do, rather than leaving the
            # operator to guess from "not found in known_hosts" whether the host
            # is down, misspelled, or simply unregistered.
            text = str(e)
            if "known_hosts" in text or "not found in" in text:
                return {
                    "success": False,
                    "error": (
                        f"The host key for {hostname} is not in your known_hosts, "
                        "so the connection was refused. Nothing is wrong with the "
                        "cluster — the key has simply never been reviewed."
                    ),
                    "remediation": (
                        f"Verify the fingerprint out of band, then register it:\n"
                        f"  ssh-keyscan -p {port} {hostname} >> ~/.ssh/known_hosts"
                    ),
                    "ssh_connection": ssh_connection,
                }
            return {
                "success": False,
                "error": f"SSH connection failed: {text}",
                "ssh_connection": ssh_connection
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Connection error: {str(e)}",
                "ssh_connection": ssh_connection
            }
        
        # Fetch remote kubeconfig
        logger.info("Fetching remote kubeconfig")
        stdin, stdout, stderr = ssh.exec_command("cat ~/.kube/config")
        
        remote_config = stdout.read().decode('utf-8')
        error_output = stderr.read().decode('utf-8')
        
        ssh.close()
        
        if not remote_config or "apiVersion" not in remote_config:
            return {
                "success": False,
                "error": f"Invalid kubeconfig received from remote host. Error: {error_output}",
                "ssh_connection": ssh_connection
            }
        
        # Get local kubeconfig path
        kubeconfig_path = settings.kubeconfig_path_resolved or Path.home() / ".kube" / "config"
        
        # Ensure .kube directory exists
        kubeconfig_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Create backup
        backup_path = kubeconfig_path.parent / f"config.backup.{context_name}"
        if kubeconfig_path.exists():
            import shutil
            shutil.copy(kubeconfig_path, backup_path)
            logger.info(f"Created backup at {backup_path}")
        
        # Write remote config to a temporary file
        temp_config = kubeconfig_path.parent / f"config.{context_name}.tmp"
        temp_config.write_text(remote_config)
        
        # Merge configs using kubectl
        merge_cmd = [
            "kubectl", "config", "view", "--flatten"
        ]
        
        # Set KUBECONFIG to merge both configs
        env = {
            "KUBECONFIG": f"{kubeconfig_path}:{temp_config}"
        }
        
        merge_result = subprocess.run(
            merge_cmd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={**subprocess.os.environ, **env}
        )
        
        if merge_result.returncode != 0:
            temp_config.unlink()
            return {
                "success": False,
                "error": f"Failed to merge kubeconfig: {merge_result.stderr}",
                "ssh_connection": ssh_connection
            }
        
        # Write merged config
        kubeconfig_path.write_text(merge_result.stdout)
        
        # Clean up temp file
        temp_config.unlink()
        
        # Rename context if needed
        if context_name != hostname:
            rename_cmd = [
                "kubectl", "config", "rename-context",
                hostname, context_name
            ]
            subprocess.run(rename_cmd, capture_output=True, timeout=5)
        
        logger.info(f"Successfully added context: {context_name}")
        
        return {
            "success": True,
            "context_name": context_name,
            "ssh_connection": ssh_connection,
            "auth_method": "password" if password else "key",
            "message": f"Successfully added kubeconfig context '{context_name}'",
            "backup_created": str(backup_path) if backup_path.exists() else None
        }
        
    except ImportError:
        return {
            "success": False,
            "error": "paramiko library not installed. Run: pip install paramiko",
            "ssh_connection": ssh_connection
        }
    except Exception as e:
        logger.exception(f"Error adding kubeconfig context: {e}")
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}",
            "ssh_connection": ssh_connection
        }


def list_kubeconfig_contexts() -> Dict[str, Any]:
    """
    List all available kubeconfig contexts.
    
    Returns:
        Dict with list of contexts and current context
    """
    try:
        runner = get_runner()
        # Get all contexts
        result = runner.run(
            ["config", "get-contexts", "-o", "name"],
            max_output=1024 * 1024
        )
        
        if not result.success:
            return {
                "success": False,
                "error": f"Failed to list contexts: {result.stderr}"
            }
        
        contexts = [ctx.strip() for ctx in result.stdout.strip().split('\n') if ctx.strip()]
        
        # Get current context
        if hasattr(runner, "context") and runner.context:
            current_context = runner.context
        else:
            current_result = runner.run(
                ["config", "current-context"],
                max_output=1024 * 1024
            )
            current_context = current_result.stdout.strip() if current_result.success else None
        
        return {
            "success": True,
            "contexts": contexts,
            "current_context": current_context,
            "total_contexts": len(contexts)
        }
        
    except Exception as e:
        logger.exception(f"Error listing contexts: {e}")
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }


def switch_kubeconfig_context(context_name: str) -> Dict[str, Any]:
    """
    Switch to a different kubeconfig context.
    
    Args:
        context_name: Name of the context to switch to
        
    Returns:
        Dict with operation result
    """
    # Validate context name (allow alphanumeric, hyphens, dots, underscores, and @ symbol)
    if not re.match(r'^[\w\-\.@]+$', context_name):
        return {
            "success": False,
            "error": "Invalid context name format"
        }
    
    try:
        runner = get_runner()
        old_context = getattr(runner, "context", None)
        
        # Temporarily clear the context on the runner so the config command
        # is run against the default context, rather than having a duplicate context flag.
        if hasattr(runner, "context"):
            runner.context = None

        try:
            result = runner.run(["config", "use-context", context_name])
            if not result.success:
                if hasattr(runner, "context"):
                    runner.context = old_context
                return {
                    "success": False,
                    "error": f"Failed to switch context: {result.stderr}",
                    "context_name": context_name
                }
        except Exception:
            if hasattr(runner, "context"):
                runner.context = old_context
            raise

        # Success: update context on the runner
        if hasattr(runner, "context"):
            runner.context = context_name

        logger.info(f"Switched to context: {context_name}")
        
        return {
            "success": True,
            "context_name": context_name,
            "message": f"Switched to context '{context_name}'"
        }
        
    except Exception as e:
        logger.exception(f"Error switching context: {e}")
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}",
            "context_name": context_name
        }


def get_current_context() -> Dict[str, Any]:
    """
    Get the current active kubeconfig context.
    
    Returns:
        Dict with current context information
    """
    try:
        runner = get_runner()
        
        # If the runner explicitly has a context set, use it as authoritative
        if hasattr(runner, "context") and runner.context:
            current_context = runner.context
        else:
            # Get current context from config
            result = runner.run(["config", "current-context"])
            if not result.success:
                return {
                    "success": False,
                    "error": "No active context or kubectl config error",
                    "current_context": None
                }
            current_context = result.stdout.strip()
        
        # Get context details
        details_result = runner.run(["config", "get-contexts", current_context])
        
        # Get namespace for current context
        namespace_result = runner.run(["config", "view", "--minify", "--output", "jsonpath={.contexts[0].context.namespace}"])
        namespace = namespace_result.stdout.strip() if namespace_result.success else None
        
        # Get cluster and user info
        cluster_result = runner.run(["config", "view", "--minify", "--output", "jsonpath={.contexts[0].context.cluster}"])
        user_result = runner.run(["config", "view", "--minify", "--output", "jsonpath={.contexts[0].context.user}"])
        
        cluster = cluster_result.stdout.strip() if cluster_result.success else None
        user = user_result.stdout.strip() if user_result.success else None
        
        return {
            "success": True,
            "current_context": current_context,
            "namespace": namespace or "default",
            "cluster": cluster,
            "user": user,
            "details": details_result.stdout if details_result.success else None
        }
        
    except Exception as e:
        logger.exception(f"Error getting current context: {e}")
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}",
            "current_context": None
        }


def _ensure_deployment_repo() -> Dict[str, Any]:
    """
    Ensure the deployment-provisioning repository is cloned and up to date.
    
    Returns:
        Dict with success status and repo path or error message
    """
    try:
        repo_url = settings.deployment_repo_url
        
        # Check if repo exists
        if DEPLOYMENT_REPO_PATH.exists():
            logger.info(f"Deployment repo exists at {DEPLOYMENT_REPO_PATH}, pulling latest changes")
            
            # Pull latest changes
            result = subprocess.run(
                ["git", "pull"],
                cwd=DEPLOYMENT_REPO_PATH,
                capture_output=True,
                text=True,
                timeout=30,
                check=False
            )
            
            if result.returncode != 0:
                logger.warning(f"Failed to pull latest changes: {result.stderr}")
                # Continue anyway - existing repo is better than nothing
            
            return {
                "success": True,
                "repo_path": str(DEPLOYMENT_REPO_PATH),
                "action": "updated"
            }
        else:
            logger.info(f"Cloning deployment repo to {DEPLOYMENT_REPO_PATH}")
            
            # Create parent directory
            DEPLOYMENT_REPO_PATH.parent.mkdir(parents=True, exist_ok=True)
            
            # Prepare git clone command
            clone_cmd = ["git", "clone"]
            
            # Add authentication if using HTTPS with token
            if repo_url.startswith("https://") and settings.github_token:
                # Insert token into URL
                repo_url_with_token = repo_url.replace(
                    "https://",
                    f"https://{settings.github_token}@"
                )
                clone_cmd.extend([repo_url_with_token, str(DEPLOYMENT_REPO_PATH)])
            else:
                # Use SSH or public HTTPS
                clone_cmd.extend([repo_url, str(DEPLOYMENT_REPO_PATH)])
            
            # Clone repository
            result = subprocess.run(
                clone_cmd,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # Disable interactive prompts
            )
            
            if result.returncode != 0:
                error_msg = result.stderr
                
                # Provide helpful error messages
                if "Repository not found" in error_msg or "not found" in error_msg:
                    error_msg = (
                        f"Repository not found or access denied. "
                        f"Please ensure:\n"
                        f"1. The repository URL is correct: {settings.deployment_repo_url}\n"
                        f"2. You have access to the repository\n"
                        f"3. For private repos:\n"
                        f"   - SSH: Your SSH keys are configured (~/.ssh/id_rsa or ~/.ssh/id_ed25519)\n"
                        f"   - HTTPS: Set GITHUB_TOKEN in .env file\n"
                        f"Original error: {error_msg}"
                    )
                
                return {
                    "success": False,
                    "error": error_msg,
                    "repo_url": settings.deployment_repo_url
                }
            
            logger.info(f"Successfully cloned deployment repo")
            
            return {
                "success": True,
                "repo_path": str(DEPLOYMENT_REPO_PATH),
                "action": "cloned"
            }
            
    except Exception as e:
        logger.exception(f"Error ensuring deployment repo: {e}")
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }


def search_deployment_repo(
    query: str,
    path_filter: Optional[str] = None,
    file_extension: Optional[str] = None
) -> Dict[str, Any]:
    """
    Search for files and content in the deployment-provisioning repository.
    
    Args:
        query: Search query (e.g., 'ansible playbook', 'helm chart')
        path_filter: Optional path filter (e.g., 'ansible/', 'helm/')
        file_extension: Optional file extension filter (e.g., '.yaml', '.yml')
        
    Returns:
        Dict with search results
    """
    # Validate query
    if not query or len(query) > 200:
        return {
            "success": False,
            "error": "Invalid query. Must be between 1 and 200 characters."
        }
    
    # Ensure repo is available
    repo_status = _ensure_deployment_repo()
    if not repo_status["success"]:
        return repo_status
    
    try:
        # Build grep command for content search
        grep_cmd = ["grep", "-r", "-i", "-n", query, str(DEPLOYMENT_REPO_PATH)]
        
        # Add file extension filter if provided
        if file_extension:
            if not file_extension.startswith('.'):
                file_extension = f'.{file_extension}'
            grep_cmd.extend(["--include", f"*{file_extension}"])
        
        # Run grep
        result = subprocess.run(
            grep_cmd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False
        )
        
        # Parse results
        matches = []
        if result.stdout:
            for line in result.stdout.split('\n')[:100]:  # Limit to 100 matches
                if ':' in line:
                    parts = line.split(':', 2)
                    if len(parts) >= 3:
                        file_path = parts[0].replace(str(DEPLOYMENT_REPO_PATH) + '/', '')
                        line_num = parts[1]
                        content = parts[2].strip()
                        
                        # Apply path filter if provided
                        if path_filter and not file_path.startswith(path_filter):
                            continue
                        
                        matches.append({
                            "file": file_path,
                            "line": line_num,
                            "content": content[:200]  # Truncate long lines
                        })
        
        # Also search for matching file names
        find_cmd = ["find", str(DEPLOYMENT_REPO_PATH), "-type", "f", "-iname", f"*{query}*"]
        
        if file_extension:
            find_cmd.extend(["-name", f"*{file_extension}"])
        
        find_result = subprocess.run(
            find_cmd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False
        )
        
        matching_files = []
        if find_result.stdout:
            for file_path in find_result.stdout.split('\n')[:50]:  # Limit to 50 files
                if file_path:
                    rel_path = file_path.replace(str(DEPLOYMENT_REPO_PATH) + '/', '')
                    
                    # Apply path filter if provided
                    if path_filter and not rel_path.startswith(path_filter):
                        continue
                    
                    matching_files.append(rel_path)
        
        return {
            "success": True,
            "query": query,
            "path_filter": path_filter,
            "file_extension": file_extension,
            "content_matches": matches,
            "matching_files": matching_files,
            "total_content_matches": len(matches),
            "total_matching_files": len(matching_files),
            "repo_path": str(DEPLOYMENT_REPO_PATH)
        }
        
    except Exception as e:
        logger.exception(f"Error searching deployment repo: {e}")
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}",
            "query": query
        }


def get_deployment_repo_file(file_path: str) -> Dict[str, Any]:
    """
    Get the contents of a file from the deployment-provisioning repository.
    
    Args:
        file_path: Relative path to file in the repository
        
    Returns:
        Dict with file contents
    """
    # Validate file path (security check)
    if not file_path or '..' in file_path or file_path.startswith('/'):
        return {
            "success": False,
            "error": "Invalid file path. Must be a relative path without '..' or leading '/'."
        }
    
    # Ensure repo is available
    repo_status = _ensure_deployment_repo()
    if not repo_status["success"]:
        return repo_status
    
    try:
        full_path = DEPLOYMENT_REPO_PATH / file_path
        
        # Security check - ensure path is within repo
        if not str(full_path.resolve()).startswith(str(DEPLOYMENT_REPO_PATH.resolve())):
            return {
                "success": False,
                "error": "Invalid file path. Path must be within the repository."
            }
        
        # Check if file exists
        if not full_path.exists():
            return {
                "success": False,
                "error": f"File not found: {file_path}"
            }
        
        # Check if it's a file (not a directory)
        if not full_path.is_file():
            return {
                "success": False,
                "error": f"Path is not a file: {file_path}"
            }
        
        # Read file content
        content = full_path.read_text()
        
        # Truncate if too large
        max_size = 50000  # 50KB
        truncated = False
        if len(content) > max_size:
            content = content[:max_size]
            truncated = True
        
        return {
            "success": True,
            "file_path": file_path,
            "content": content,
            "size_bytes": full_path.stat().st_size,
            "truncated": truncated,
            "repo_path": str(DEPLOYMENT_REPO_PATH)
        }
        
    except Exception as e:
        logger.exception(f"Error reading file from deployment repo: {e}")
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}",
            "file_path": file_path
        }


def list_deployment_repo_path(path: str = "") -> Dict[str, Any]:
    """
    List files and directories in a path within the deployment-provisioning repository.
    
    Args:
        path: Relative path in the repository (default: root)
        
    Returns:
        Dict with directory listing
    """
    # Validate path (security check)
    if '..' in path or (path and path.startswith('/')):
        return {
            "success": False,
            "error": "Invalid path. Must be a relative path without '..' or leading '/'."
        }
    
    # Ensure repo is available
    repo_status = _ensure_deployment_repo()
    if not repo_status["success"]:
        return repo_status
    
    try:
        full_path = DEPLOYMENT_REPO_PATH / path if path else DEPLOYMENT_REPO_PATH
        
        # Security check - ensure path is within repo
        if not str(full_path.resolve()).startswith(str(DEPLOYMENT_REPO_PATH.resolve())):
            return {
                "success": False,
                "error": "Invalid path. Path must be within the repository."
            }
        
        # Check if path exists
        if not full_path.exists():
            return {
                "success": False,
                "error": f"Path not found: {path}"
            }
        
        # Check if it's a directory
        if not full_path.is_dir():
            return {
                "success": False,
                "error": f"Path is not a directory: {path}"
            }
        
        # List directory contents
        directories = []
        files = []
        
        for item in sorted(full_path.iterdir()):
            rel_path = str(item.relative_to(DEPLOYMENT_REPO_PATH))
            
            if item.is_dir():
                directories.append({
                    "name": item.name,
                    "path": rel_path,
                    "type": "directory"
                })
            else:
                files.append({
                    "name": item.name,
                    "path": rel_path,
                    "type": "file",
                    "size_bytes": item.stat().st_size
                })
        
        return {
            "success": True,
            "path": path or "/",
            "directories": directories,
            "files": files,
            "total_directories": len(directories),
            "total_files": len(files),
            "repo_path": str(DEPLOYMENT_REPO_PATH)
        }
        
    except Exception as e:
        logger.exception(f"Error listing deployment repo path: {e}")
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}",
            "path": path
        }


def exec_pod_command(
    namespace: str,
    pod_name: str,
    command: str,
    container: Optional[str] = None,
    confirm: bool = False
) -> Dict[str, Any]:
    """
    Execute a command in a pod container.
    
    WRITE OPERATION: Requires confirm=True to execute.
    
    Args:
        namespace: Namespace containing the pod
        pod_name: Name of the pod
        command: Command to execute
        container: Optional container name for multi-container pods
        confirm: Must be True to execute (safety guard)
        
    Returns:
        Dict with command output or error
    """
    # Check if recovery operations are enabled
    if not settings.enable_recovery_operations:
        return {
            "success": False,
            "error": "Recovery operations are disabled. Set ENABLE_RECOVERY_OPERATIONS=true in .env to enable.",
            "operation": "exec_pod_command"
        }
    
    namespace = validate_namespace(namespace)
    pod_name = validate_resource_name(pod_name, "pod")
    
    # SAFETY: Require explicit confirmation
    if not confirm:
        return {
            "success": False,
            "error": "Confirmation required. Set confirm=True to execute this command.",
            "namespace": namespace,
            "pod_name": pod_name,
            "command": command,
            "requires_approval": True,
            "operation": "exec_pod_command"
        }
    
    # SECURITY: Validate command (basic sanity check)
    if not command or len(command) > 1000:
        return {
            "success": False,
            "error": "Invalid command. Must be between 1 and 1000 characters."
        }
    
    # Build kubectl exec command
    args = ["exec", pod_name, "--"]
    
    if container:
        # Validate container name
        if not container or len(container) > 253:
            return {
                "success": False,
                "error": f"Invalid container name: {container}"
            }
        if not all(c.isalnum() or c in '-_.' for c in container):
            return {
                "success": False,
                "error": f"Invalid container name: '{container}'. Must contain only alphanumeric characters, hyphens, dots, and underscores."
            }
        args.insert(2, "-c")
        args.insert(3, container)
    
    # Add command (split by spaces for safety)
    args.extend(command.split())
    
    try:
        result = get_runner().run(args, namespace=namespace)
        
        return {
            "success": True,
            "namespace": namespace,
            "pod_name": pod_name,
            "container": container,
            "command": command,
            "output": result.stdout,
            "stderr": result.stderr if result.stderr else None,
            "truncated": result.truncated,
            "operation": "exec_pod_command"
        }
        
    except KubectlError as e:
        return {
            "success": False,
            "namespace": namespace,
            "pod_name": pod_name,
            "command": command,
            "error": str(e),
            "stderr": e.stderr,
            "operation": "exec_pod_command"
        }


def _disabled_response(operation: str) -> Dict[str, Any]:
    return {
        "success": False,
        "error": "Recovery operations are disabled. Set ENABLE_RECOVERY_OPERATIONS=true in .env to enable.",
        "operation": operation,
    }


def _run_dry_preview(
    operation: str,
    kubectl_args: List[str],
    namespace: Optional[str],
    fingerprint_kwargs: Dict[str, Any],
    response_base: Dict[str, Any],
    user: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute `kubectl ... --dry-run=server`, mint a confirmation token, and
    return a preview response. Shared by every destructive wrapper."""
    from services.confirmation import token_store, fingerprint, audit_token_event

    preview_args = list(kubectl_args) + ["--dry-run=server"]
    try:
        result = get_runner().run(preview_args, namespace=namespace)
        preview_text = result.stdout.strip()
        preview_stderr = result.stderr.strip() if hasattr(result, "stderr") else ""
    except KubectlError as e:
        return {
            **response_base,
            "success": False,
            "dry_run": True,
            "error": f"dry-run failed: {e}",
            "stderr": getattr(e, "stderr", ""),
        }

    fp = fingerprint(operation, **fingerprint_kwargs)
    token_value: Optional[str] = None
    ttl = 0
    if settings.require_destructive_confirmation:
        ttl = settings.confirmation_token_ttl_seconds
        record = token_store.issue(operation=operation, fingerprint=fp, ttl_seconds=ttl, user=user)
        token_value = record.token
        audit_token_event(
            event="issued",
            operation=operation,
            target_fingerprint=fp,
            token_prefix=token_value,
            user=record.issued_to,
        )

    return {
        **response_base,
        "success": True,
        "dry_run": True,
        "preview": preview_text or preview_stderr or "(no output from --dry-run=server)",
        "confirmation_token": token_value,
        "confirmation_required": settings.require_destructive_confirmation,
        "expires_in_seconds": ttl if token_value else None,
        "operation": operation,
    }


def _require_confirmation_token(
    operation: str,
    confirmation_token: Optional[str],
    fingerprint_kwargs: Dict[str, Any],
    response_base: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Validate the confirmation token in strict mode.

    Returns None when validation passes (caller may proceed) or an error-shaped
    response dict when validation fails. When `require_destructive_confirmation`
    is False, returns None unconditionally (legacy mode).
    """
    if not settings.require_destructive_confirmation:
        return None

    from services.confirmation import token_store, fingerprint, audit_token_event

    fp = fingerprint(operation, **fingerprint_kwargs)
    record, reason = token_store.consume(
        token=confirmation_token or "",
        operation=operation,
        fingerprint=fp,
    )
    if record is None:
        audit_token_event(
            event="rejected",
            operation=operation,
            target_fingerprint=fp,
            token_prefix=confirmation_token or "",
            reason=reason,
        )
        return {
            **response_base,
            "success": False,
            "operation": operation,
            "error": (
                "confirmation_token is required for destructive operations. "
                "Call with dry_run=True first to preview and receive a single-use token."
            ),
            "token_error": reason,
            "requires_approval": True,
        }

    audit_token_event(
        event="consumed",
        operation=operation,
        target_fingerprint=fp,
        token_prefix=confirmation_token or "",
        user=record.issued_to,
    )
    return None


def delete_pod(
    namespace: str,
    pod_name: str,
    grace_period: int = 30,
    confirm: bool = False,
    dry_run: bool = False,
    confirmation_token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Delete a pod (forces restart for pods managed by controllers).

    DESTRUCTIVE OPERATION. Two-step ritual when require_destructive_confirmation
    is enabled (default):
      1. Call with dry_run=True → returns preview + single-use confirmation_token
      2. Call with confirm=True + that confirmation_token → executes the delete

    Args:
        namespace: Namespace containing the pod
        pod_name: Name of the pod to delete
        grace_period: Grace period in seconds (default: 30)
        confirm: Must be True to execute (legacy safety guard)
        dry_run: When True, runs kubectl --dry-run=server and returns a preview
        confirmation_token: Token from a prior dry_run call (required in strict mode)
    """
    if not settings.enable_recovery_operations:
        return _disabled_response("delete_pod")

    namespace = validate_namespace(namespace)
    pod_name = validate_resource_name(pod_name, "pod")

    if grace_period < 0 or grace_period > settings.max_grace_period_seconds:
        return {
            "success": False,
            "error": f"Grace period must be between 0 and {settings.max_grace_period_seconds} seconds.",
            "operation": "delete_pod",
        }

    args = ["delete", "pod", pod_name, f"--grace-period={grace_period}"]
    response_base = {"namespace": namespace, "pod_name": pod_name, "grace_period": grace_period}
    fp_kwargs = {"namespace": namespace, "pod_name": pod_name, "grace_period": grace_period}

    if dry_run:
        return _run_dry_preview("delete_pod", args, namespace, fp_kwargs, response_base)

    if not confirm:
        return {
            **response_base,
            "success": False,
            "error": "Confirmation required. Set confirm=True (and pass a confirmation_token in strict mode) to delete this pod.",
            "requires_approval": True,
            "operation": "delete_pod",
            "warning": "This will delete the pod. If managed by a controller (Deployment, StatefulSet), it will be recreated.",
        }

    token_error = _require_confirmation_token("delete_pod", confirmation_token, fp_kwargs, response_base)
    if token_error is not None:
        return token_error

    try:
        result = get_runner().run(args, namespace=namespace)
        return {
            **response_base,
            "success": True,
            "message": result.stdout.strip(),
            "operation": "delete_pod",
        }
    except KubectlError as e:
        return {
            **response_base,
            "success": False,
            "error": str(e),
            "stderr": e.stderr,
            "operation": "delete_pod",
        }


def rollout_restart(
    namespace: str,
    deployment_name: str,
    confirm: bool = False,
    dry_run: bool = False,
    confirmation_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Restart a deployment (rolling restart of all pods). See delete_pod for the
    dry_run / confirmation_token ritual."""
    if not settings.enable_recovery_operations:
        return _disabled_response("rollout_restart")

    namespace = validate_namespace(namespace)
    deployment_name = validate_resource_name(deployment_name, "deployment")

    args = ["rollout", "restart", f"deployment/{deployment_name}"]
    response_base = {"namespace": namespace, "deployment_name": deployment_name}
    fp_kwargs = {"namespace": namespace, "deployment_name": deployment_name}

    if dry_run:
        return _run_dry_preview("rollout_restart", args, namespace, fp_kwargs, response_base)

    if not confirm:
        return {
            **response_base,
            "success": False,
            "error": "Confirmation required. Set confirm=True (and pass a confirmation_token in strict mode) to restart this deployment.",
            "requires_approval": True,
            "operation": "rollout_restart",
            "warning": "This will perform a rolling restart of all pods in the deployment.",
        }

    token_error = _require_confirmation_token("rollout_restart", confirmation_token, fp_kwargs, response_base)
    if token_error is not None:
        return token_error

    try:
        result = get_runner().run(args, namespace=namespace)
        return {
            **response_base,
            "success": True,
            "message": result.stdout.strip(),
            "operation": "rollout_restart",
            "next_step": "Use get_rollout_status to monitor the restart progress",
        }
    except KubectlError as e:
        return {
            **response_base,
            "success": False,
            "error": str(e),
            "stderr": e.stderr,
            "operation": "rollout_restart",
        }


def scale_deployment(
    namespace: str,
    deployment_name: str,
    replicas: int,
    confirm: bool = False,
    dry_run: bool = False,
    confirmation_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Scale a deployment to a specific number of replicas. See delete_pod for the
    dry_run / confirmation_token ritual."""
    if not settings.enable_recovery_operations:
        return _disabled_response("scale_deployment")

    namespace = validate_namespace(namespace)
    deployment_name = validate_resource_name(deployment_name, "deployment")

    if replicas < 0 or replicas > settings.max_scale_replicas:
        return {
            "success": False,
            "error": f"Replicas must be between 0 and {settings.max_scale_replicas}.",
            "operation": "scale_deployment",
        }

    args = ["scale", f"deployment/{deployment_name}", f"--replicas={replicas}"]
    response_base = {
        "namespace": namespace,
        "deployment_name": deployment_name,
        "target_replicas": replicas,
    }
    fp_kwargs = {
        "namespace": namespace,
        "deployment_name": deployment_name,
        "replicas": replicas,
    }

    if dry_run:
        return _run_dry_preview("scale_deployment", args, namespace, fp_kwargs, response_base)

    if not confirm:
        return {
            **response_base,
            "success": False,
            "error": "Confirmation required. Set confirm=True (and pass a confirmation_token in strict mode) to scale this deployment.",
            "requires_approval": True,
            "operation": "scale_deployment",
            "warning": f"This will scale the deployment to {replicas} replicas.",
        }

    token_error = _require_confirmation_token("scale_deployment", confirmation_token, fp_kwargs, response_base)
    if token_error is not None:
        return token_error

    try:
        result = get_runner().run(args, namespace=namespace)
        return {
            **response_base,
            "success": True,
            "message": result.stdout.strip(),
            "operation": "scale_deployment",
            "next_step": "Use get_deployment to verify the scaling operation",
        }
    except KubectlError as e:
        return {
            **response_base,
            "success": False,
            "error": str(e),
            "stderr": e.stderr,
            "operation": "scale_deployment",
        }


def apply_patch(
    namespace: str,
    resource_type: str,
    resource_name: str,
    patch: str,
    patch_type: str = "strategic",
    confirm: bool = False,
    dry_run: bool = False,
    confirmation_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply a patch to a Kubernetes resource. See delete_pod for the
    dry_run / confirmation_token ritual."""
    if not settings.enable_recovery_operations:
        return _disabled_response("apply_patch")

    namespace = validate_namespace(namespace)
    resource_name = validate_resource_name(resource_name, resource_type)

    allowed_resource_types = [
        "deployment", "statefulset", "daemonset", "pod",
        "service", "configmap", "secret"
    ]
    if resource_type.lower() not in allowed_resource_types:
        return {
            "success": False,
            "error": f"Invalid resource type. Allowed: {', '.join(allowed_resource_types)}",
            "operation": "apply_patch",
        }

    if patch_type not in ["strategic", "merge", "json"]:
        return {
            "success": False,
            "error": "Invalid patch type. Must be 'strategic', 'merge', or 'json'.",
            "operation": "apply_patch",
        }

    import json as _json
    try:
        _json.loads(patch)
    except _json.JSONDecodeError as e:
        return {
            "success": False,
            "error": f"Invalid JSON patch: {str(e)}",
            "operation": "apply_patch",
        }

    args = ["patch", resource_type, resource_name, "--type", patch_type, "--patch", patch]
    response_base = {
        "namespace": namespace,
        "resource_type": resource_type,
        "resource_name": resource_name,
        "patch": patch,
        "patch_type": patch_type,
    }
    # Hash the patch body so a token issued for one diff can't execute a different one.
    patch_digest = hashlib.sha256(patch.encode("utf-8")).hexdigest()[:16]
    fp_kwargs = {
        "namespace": namespace,
        "resource_type": resource_type,
        "resource_name": resource_name,
        "patch_type": patch_type,
        "patch_digest": patch_digest,
    }

    if dry_run:
        return _run_dry_preview("apply_patch", args, namespace, fp_kwargs, response_base)

    if not confirm:
        return {
            **response_base,
            "success": False,
            "error": "Confirmation required. Set confirm=True (and pass a confirmation_token in strict mode) to apply this patch.",
            "requires_approval": True,
            "operation": "apply_patch",
            "warning": "This will modify the resource configuration.",
        }

    token_error = _require_confirmation_token("apply_patch", confirmation_token, fp_kwargs, response_base)
    if token_error is not None:
        return token_error

    try:
        result = get_runner().run(args, namespace=namespace)
        return {
            **response_base,
            "success": True,
            "message": result.stdout.strip(),
            "operation": "apply_patch",
            "next_step": f"Use get_{resource_type} to verify the patch was applied",
        }
    except KubectlError as e:
        return {
            **response_base,
            "success": False,
            "error": str(e),
            "stderr": e.stderr,
            "operation": "apply_patch",
        }

def get_resource_graph(namespace: str) -> Dict[str, Any]:
    """
    Generate a resource graph for a given namespace.
    Fetches Deployments, Services, Ingresses, and Pods, and maps their relationships.
    """
    validate_namespace(namespace)
    
    nodes = []
    edges = []
    
    try:
        # Fetch resources
        runner = get_runner()
        deploys_json = runner.run_json(["get", "deployments", "-o", "json"], namespace=namespace)
        svcs_json = runner.run_json(["get", "services", "-o", "json"], namespace=namespace)
        
        # Ingress API group might vary, usually networking.k8s.io/v1
        try:
            ings_json = runner.run_json(["get", "ingresses", "-o", "json"], namespace=namespace)
        except Exception:
            ings_json = {"items": []}
            
        pods_json = runner.run_json(["get", "pods", "-o", "json"], namespace=namespace)
        
        deployments = deploys_json.get("items", [])
        services = svcs_json.get("items", [])
        ingresses = ings_json.get("items", [])
        pods = pods_json.get("items", [])
        
        # Add Nodes
        for d in deployments:
            name = d["metadata"]["name"]
            ready = d.get("status", {}).get("readyReplicas", 0)
            total = d.get("spec", {}).get("replicas", 0)
            nodes.append({"id": f"deploy-{name}", "label": name, "type": "deployment", "status": f"{ready}/{total} ready"})
            
        for s in services:
            name = s["metadata"]["name"]
            nodes.append({"id": f"svc-{name}", "label": name, "type": "service", "status": s.get("spec", {}).get("type", "Unknown")})
            
        for i in ingresses:
            name = i["metadata"]["name"]
            nodes.append({"id": f"ing-{name}", "label": name, "type": "ingress", "status": ""})
            
        for p in pods:
            name = p["metadata"]["name"]
            phase = p.get("status", {}).get("phase", "Unknown")
            nodes.append({"id": f"pod-{name}", "label": name, "type": "pod", "status": phase})
            
        # Build Edges
        # 1. Ingress -> Service
        for i in ingresses:
            ing_name = i["metadata"]["name"]
            rules = i.get("spec", {}).get("rules", [])
            for r in rules:
                paths = r.get("http", {}).get("paths", [])
                for p in paths:
                    svc_name = p.get("backend", {}).get("service", {}).get("name")
                    if svc_name:
                        edges.append({"source": f"ing-{ing_name}", "target": f"svc-{svc_name}"})
                        
        # 2. Service -> Pods (Label matching)
        for s in services:
            svc_name = s["metadata"]["name"]
            selector = s.get("spec", {}).get("selector", {})
            if selector:
                for p in pods:
                    pod_labels = p.get("metadata", {}).get("labels", {})
                    if all(pod_labels.get(k) == v for k, v in selector.items()):
                        edges.append({"source": f"svc-{svc_name}", "target": f"pod-{p['metadata']['name']}"})
                        
        # 3. Deployment -> Pods (Label matching)
        for d in deployments:
            dep_name = d["metadata"]["name"]
            match_labels = d.get("spec", {}).get("selector", {}).get("matchLabels", {})
            if match_labels:
                for p in pods:
                    pod_labels = p.get("metadata", {}).get("labels", {})
                    if all(pod_labels.get(k) == v for k, v in match_labels.items()):
                        edges.append({"source": f"deploy-{dep_name}", "target": f"pod-{p['metadata']['name']}"})

        # Deduplicate edges
        unique_edges = [dict(t) for t in {tuple(e.items()) for e in edges}]

        return {
            "success": True,
            "namespace": namespace,
            "nodes": nodes,
            "edges": unique_edges
        }

    except Exception as e:
        logger.error(f"Failed to generate resource graph: {e}")
        return {
            "success": False,
            "error": str(e),
            "namespace": namespace,
            "nodes": [],
            "edges": []
        }

def investigate_workload(
    namespace: str,
    workload_name: str,
    workload_type: str = "deployment",
    use_ai: bool = True
) -> Dict[str, Any]:
    """
    Run an investigation for a workload (Deployment, StatefulSet, DaemonSet).
    Gathers definition, related pods, events, and runs AI analysis if enabled.
    """
    namespace = validate_namespace(namespace)
    workload_name = validate_resource_name(workload_name, workload_type)
    
    result: Dict[str, Any] = {
        "success": True,
        "namespace": namespace,
        "workload_name": workload_name,
        "workload_type": workload_type,
        "steps_run": [],
    }

    runner = get_runner()
    
    # 1. Get the workload
    try:
        wl_json = runner.run_json(["get", workload_type, workload_name, "-o", "json"], namespace=namespace)
        
        # Strip large noisy fields
        import json
        if "managedFields" in wl_json.get("metadata", {}):
            del wl_json["metadata"]["managedFields"]
            
        result["describe"] = json.dumps(wl_json, indent=2)
        result["workload_summary"] = _workload_summary(wl_json, workload_type)
        result["steps_run"].append(f"get_{workload_type}")
    except KubectlError as e:
        return {
            "success": False,
            "error": str(e),
            "namespace": namespace,
            "workload_name": workload_name,
            "operation": "investigate_workload",
        }

    # 2. Get related pods (via selector)
    match_labels = _selector_match_labels(wl_json.get("spec", {}).get("selector", {}))
    if match_labels:
        selector_str = ",".join(f"{k}={v}" for k, v in match_labels.items())
        try:
            pods_json = runner.run_json(["get", "pods", "-l", selector_str, "-o", "json"], namespace=namespace)
            result["pods"] = pods_json.get("items", [])
            parsed_pods = parse_pod_list(pods_json)
            result["related_pods_summary"] = {
                "selector": selector_str,
                "pod_count": len(parsed_pods),
                "pods": parsed_pods,
                "status_breakdown": {
                    status: sum(1 for pod in parsed_pods if pod.get("status") == status)
                    for status in sorted({pod.get("status", "Unknown") for pod in parsed_pods})
                },
            }
            result["steps_run"].append("get_pods")
        except KubectlError:
            result["pods"] = []
            result["related_pods_summary"] = {"selector": selector_str, "pod_count": 0, "pods": []}
    else:
        result["pods"] = []
        result["related_pods_summary"] = {"selector": "", "pod_count": 0, "pods": []}

    # 3. Get related events
    try:
        events_json = runner.run_json(
            ["get", "events", "--field-selector", f"involvedObject.name={workload_name}", "-o", "json"],
            namespace=namespace
        )
        result["events"] = events_json
        parsed_events = parse_events(events_json)
        result["events_parsed"] = {
            "event_count": len(parsed_events),
            "events": parsed_events[:50],
        }
        result["steps_run"].append("get_events")
    except KubectlError:
        result["events"] = {}
        result["events_parsed"] = {"event_count": 0, "events": []}

    # 4. AI Analysis
    if use_ai and _ai_service_available and _llm_service:
        try:
            ai_result = _llm_service.analyze_workload_investigation(workload_name, namespace, result)
            result["ai"] = ai_result
            result["steps_run"].append("ai_analysis")
        except Exception as e:
            logger.warning(f"AI workload analysis failed: {e}")
            result["ai"] = {"ai_enabled": False, "error": str(e)}
    elif use_ai:
        result["ai"] = {"ai_enabled": False, "message": "AI service not available"}

    return result

def _namespace_issue_summary(namespace: str, resources: Dict[str, Any], events: Dict[str, Any]) -> Dict[str, Any]:
    def _matches_selector(labels: dict, selector: dict) -> bool:
        if not selector:
            return False
        return all(labels.get(key) == value for key, value in selector.items())

    unhealthy_pods = [
        {
            "namespace": pod.get("namespace", namespace),
            "name": pod.get("name", ""),
            "status": pod.get("status", ""),
            "ready": pod.get("ready"),
            "restarts": pod.get("restarts", 0),
        }
        for pod in resources.get("pods", []) or []
        if pod.get("status") not in ("Running", "Succeeded") or pod.get("ready") is False
    ]

    unavailable_workloads = []
    for kind, desired_key in (
        ("deployments", "replicas"),
        ("statefulsets", "replicas"),
        ("daemonsets", "desired"),
    ):
        for workload in resources.get(kind, []) or []:
            desired = workload.get(desired_key, 0) or 0
            ready = workload.get("ready", 0) or 0
            unavailable = workload.get("unavailable", max(desired - ready, 0))
            if desired and ready < desired:
                unavailable_workloads.append({
                    "kind": kind[:-1],
                    "name": workload.get("name", ""),
                    "namespace": workload.get("namespace", namespace),
                    "desired": desired,
                    "ready": ready,
                    "unavailable": unavailable,
                })

    warning_event_groups: dict[tuple[str, str, str], dict] = {}
    for event in events.get("events", []) or []:
        if event.get("type") and event.get("type") != "Warning":
            continue
        involved = event.get("involved_object", {}) or event.get("involvedObject", {}) or {}
        key = (
            event.get("reason", ""),
            involved.get("kind", ""),
            involved.get("name", ""),
        )
        group = warning_event_groups.setdefault(key, {
            "reason": key[0],
            "object_kind": key[1],
            "object_name": key[2],
            "count": 0,
            "messages": [],
        })
        group["count"] += event.get("count", 1) or 1
        message = event.get("message", "")
        if message and message not in group["messages"] and len(group["messages"]) < 3:
            group["messages"].append(message)

    service_endpoint_checks = []
    for service in resources.get("services", []) or []:
        selector = service.get("selector", {}) or {}
        if not selector:
            service_endpoint_checks.append({
                "service": service.get("name", ""),
                "selector": selector,
                "has_selector": False,
                "has_ready_endpoints": False,
                "reason": "service has no selector",
            })
            continue
        matching_pods = [
            pod for pod in resources.get("pods", []) or []
            if _matches_selector(pod.get("labels", {}) or {}, selector)
        ]
        try:
            endpoint_result = get_endpoints(namespace, service.get("name", ""))
            endpoint_slices = endpoint_result.get("endpoint_slices") or {}
            endpoint_slice_ready_count = endpoint_slices.get("ready_count", 0) or 0
            ready_count = endpoint_result.get("ready_count", 0) or 0
            has_ready_endpoints = bool(endpoint_result.get("has_endpoints")) or ready_count > 0 or endpoint_slice_ready_count > 0
            service_endpoint_checks.append({
                "service": service.get("name", ""),
                "selector": selector,
                "has_selector": True,
                "matching_pod_count": len(matching_pods),
                "selector_matches_pods": len(matching_pods) > 0,
                "has_ready_endpoints": has_ready_endpoints,
                "ready_count": ready_count,
                "not_ready_count": endpoint_result.get("not_ready_count", 0),
                "endpoint_slice_ready_count": endpoint_slice_ready_count,
                "diagnostic_hint": endpoint_result.get("diagnostic_hint"),
            })
        except Exception as exc:
            service_endpoint_checks.append({
                "service": service.get("name", ""),
                "selector": selector,
                "has_selector": True,
                "matching_pod_count": len(matching_pods),
                "selector_matches_pods": len(matching_pods) > 0,
                "has_ready_endpoints": False,
                "error": str(exc),
            })

    services_without_ready_endpoints = [
        check for check in service_endpoint_checks
        if check.get("has_selector") and not check.get("has_ready_endpoints")
    ]
    services_with_selector_mismatch = [
        check for check in service_endpoint_checks
        if check.get("has_selector") and not check.get("selector_matches_pods")
    ]

    return {
        "unhealthy_pod_count": len(unhealthy_pods),
        "unhealthy_pods": unhealthy_pods,
        "unavailable_workload_count": len(unavailable_workloads),
        "unavailable_workloads": unavailable_workloads,
        "warning_event_group_count": len(warning_event_groups),
        "warning_event_groups": list(warning_event_groups.values()),
        "service_endpoint_checks": service_endpoint_checks,
        "services_without_ready_endpoints_count": len(services_without_ready_endpoints),
        "services_without_ready_endpoints": services_without_ready_endpoints,
        "services_with_selector_mismatch_count": len(services_with_selector_mismatch),
        "services_with_selector_mismatch": services_with_selector_mismatch,
    }


def analyze_namespace(namespace: str) -> Dict[str, Any]:
    """
    Holistic health check for a namespace. Combines resource overview with Warning events.
    Passes data to Gemini to identify systemic/cascading failures.
    """
    namespace = validate_namespace(namespace)
    
    result: Dict[str, Any] = {
        "success": True,
        "namespace": namespace,
        "steps_run": [],
    }

    # 1. Get resources overview
    resources = list_namespace_resources(namespace)
    result["resources"] = resources
    result["steps_run"].append("list_namespace_resources")

    # 2. Get warning events
    events = get_events(namespace, field_selector="type=Warning")
    result["events"] = events
    result["steps_run"].append("get_events")

    result["issue_summary"] = _namespace_issue_summary(namespace, resources, events)
    result["steps_run"].append("issue_summary")

    # 3. AI Analysis
    if _ai_service_available and _llm_service:
        try:
            ai_result = _llm_service.analyze_namespace_health(namespace, resources, events)
            result["ai"] = ai_result
            result["steps_run"].append("ai_analysis")
        except Exception as e:
            logger.warning(f"AI namespace analysis failed: {e}")
            result["ai"] = {"ai_enabled": False, "error": str(e)}
    else:
        result["ai"] = {"ai_enabled": False, "message": "AI service not available"}

    return result
