"""Parsers for kubectl output into structured formats."""

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional


def _env_value_from_summary(value_from: dict) -> Optional[Dict[str, Any]]:
    if not isinstance(value_from, dict) or not value_from:
        return None
    for key, ref_type in (
        ("secretKeyRef", "secretKeyRef"),
        ("configMapKeyRef", "configMapKeyRef"),
        ("fieldRef", "fieldRef"),
        ("resourceFieldRef", "resourceFieldRef"),
    ):
        ref = value_from.get(key)
        if isinstance(ref, dict):
            return {
                "type": ref_type,
                "name": ref.get("name", ""),
                "key": ref.get("key", ""),
                "field_path": ref.get("fieldPath", ""),
                "resource": ref.get("resource", ""),
                "optional": ref.get("optional"),
            }
    return {"type": "unknown"}


def _env_from_summary(env_from: list) -> List[Dict[str, Any]]:
    refs = []
    for item in env_from or []:
        if not isinstance(item, dict):
            continue
        prefix = item.get("prefix", "")
        if isinstance(item.get("secretRef"), dict):
            refs.append({
                "type": "secretRef",
                "name": item["secretRef"].get("name", ""),
                "prefix": prefix,
                "optional": item["secretRef"].get("optional"),
            })
        if isinstance(item.get("configMapRef"), dict):
            refs.append({
                "type": "configMapRef",
                "name": item["configMapRef"].get("name", ""),
                "prefix": prefix,
                "optional": item["configMapRef"].get("optional"),
            })
    return refs


def _container_summary(container: dict) -> Dict[str, Any]:
    env = []
    for item in container.get("env", []) or []:
        if not isinstance(item, dict):
            continue
        # Deliberately expose env names and references, never literal values.
        env.append({
            "name": item.get("name", ""),
            "value_from": _env_value_from_summary(item.get("valueFrom", {})),
            "has_literal_value": "value" in item,
        })

    return {
        "name": container.get("name", ""),
        "image": container.get("image", ""),
        "ports": [
            {
                "name": port.get("name", ""),
                "container_port": port.get("containerPort"),
                "protocol": port.get("protocol", "TCP"),
            }
            for port in container.get("ports", []) or []
            if isinstance(port, dict)
        ],
        "resources": container.get("resources", {}) or {},
        "env": env,
        "env_from": _env_from_summary(container.get("envFrom", [])),
        "volume_mounts": [
            {
                "name": mount.get("name", ""),
                "mount_path": mount.get("mountPath", ""),
                "read_only": mount.get("readOnly", False),
            }
            for mount in container.get("volumeMounts", []) or []
            if isinstance(mount, dict)
        ],
    }


def _volume_summary(volume: dict) -> Dict[str, Any]:
    for key, ref_type in (
        ("secret", "secret"),
        ("configMap", "configMap"),
        ("persistentVolumeClaim", "persistentVolumeClaim"),
        ("projected", "projected"),
        ("emptyDir", "emptyDir"),
        ("hostPath", "hostPath"),
        ("downwardAPI", "downwardAPI"),
    ):
        if key in volume:
            ref = volume.get(key) if isinstance(volume.get(key), dict) else {}
            return {
                "name": volume.get("name", ""),
                "type": ref_type,
                "ref_name": ref.get("secretName") or ref.get("name") or ref.get("claimName") or "",
                "path": ref.get("path", "") if ref_type == "hostPath" else "",
            }
    return {"name": volume.get("name", ""), "type": "unknown", "ref_name": ""}


def _affinity_summary(affinity: dict) -> Dict[str, Any]:
    if not isinstance(affinity, dict) or not affinity:
        return {}
    return {
        "has_node_affinity": bool(affinity.get("nodeAffinity")),
        "has_pod_affinity": bool(affinity.get("podAffinity")),
        "has_pod_anti_affinity": bool(affinity.get("podAntiAffinity")),
    }


def _owner_references_summary(owner_refs: list) -> List[Dict[str, Any]]:
    return [
        {
            "kind": ref.get("kind", ""),
            "name": ref.get("name", ""),
            "controller": ref.get("controller", False),
        }
        for ref in owner_refs or []
        if isinstance(ref, dict)
    ]


def _annotations_summary(annotations: dict) -> Dict[str, Any]:
    if not isinstance(annotations, dict) or not annotations:
        return {"count": 0, "keys": []}
    return {
        "count": len(annotations),
        "keys": sorted(annotations.keys()),
    }


def _pod_template_summary(template: dict) -> Dict[str, Any]:
    metadata = template.get("metadata", {}) if isinstance(template, dict) else {}
    spec = template.get("spec", {}) if isinstance(template, dict) else {}
    containers = spec.get("containers", []) or []
    init_containers = spec.get("initContainers", []) or []

    return {
        "labels": metadata.get("labels", {}) or {},
        "label_count": len(metadata.get("labels", {}) or {}),
        "annotations": _annotations_summary(metadata.get("annotations", {}) or {}),
        "service_account_name": spec.get("serviceAccountName", ""),
        "node_selector": spec.get("nodeSelector", {}) or {},
        "tolerations": [
            {
                "key": tol.get("key", ""),
                "operator": tol.get("operator", ""),
                "value": tol.get("value", ""),
                "effect": tol.get("effect", ""),
                "toleration_seconds": tol.get("tolerationSeconds"),
            }
            for tol in spec.get("tolerations", []) or []
            if isinstance(tol, dict)
        ],
        "affinity": _affinity_summary(spec.get("affinity", {})),
        "containers": [_container_summary(c) for c in containers if isinstance(c, dict)],
        "init_containers": [_container_summary(c) for c in init_containers if isinstance(c, dict)],
        "images": [
            c.get("image", "")
            for c in containers
            if isinstance(c, dict) and c.get("image")
        ],
        "volumes": [_volume_summary(v) for v in spec.get("volumes", []) or [] if isinstance(v, dict)],
    }


def parse_pod_list(json_output: dict) -> List[Dict[str, Any]]:
    """
    Parse kubectl get pods JSON output into structured summaries.
    
    Args:
        json_output: JSON dict from kubectl get pods -o json
        
    Returns:
        List of pod summary dicts with enhanced error information
    """
    if not isinstance(json_output, dict):
        return []
    
    pods = []
    
    for item in json_output.get("items", []):
        try:
            metadata = item.get("metadata", {})
            spec = item.get("spec", {})
            status = item.get("status", {})
            
            # Calculate ready containers
            container_statuses = status.get("containerStatuses", [])
            ready_count = sum(1 for cs in container_statuses if cs.get("ready", False))
            total_count = len(container_statuses) if container_statuses else 0
            
            # Calculate total restart count
            restart_count = sum(cs.get("restartCount", 0) for cs in container_statuses)
            
            # Extract container states for better diagnostics
            container_states = []
            for cs in container_statuses:
                state_info = {
                    "name": cs.get("name", "unknown"),
                    "ready": cs.get("ready", False),
                    "restart_count": cs.get("restartCount", 0),
                }
                
                # Get current state
                state = cs.get("state", {})
                if "waiting" in state:
                    state_info["state"] = "Waiting"
                    state_info["reason"] = state["waiting"].get("reason", "")
                    state_info["message"] = state["waiting"].get("message", "")
                elif "running" in state:
                    state_info["state"] = "Running"
                    state_info["started_at"] = state["running"].get("startedAt", "")
                elif "terminated" in state:
                    state_info["state"] = "Terminated"
                    state_info["reason"] = state["terminated"].get("reason", "")
                    state_info["exit_code"] = state["terminated"].get("exitCode", 0)
                else:
                    state_info["state"] = "Unknown"
                
                container_states.append(state_info)
            
            # Determine effective status by checking container waiting reasons
            # This prevents showing "Running" for pods stuck in CrashLoopBackOff
            effective_status = status.get("phase", "Unknown")
            status_reason = ""
            for cs in container_statuses:
                waiting = cs.get("state", {}).get("waiting", {})
                reason = waiting.get("reason", "")
                if reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
                              "CreateContainerConfigError", "InvalidImageName",
                              "RunContainerError"):
                    effective_status = reason
                    status_reason = waiting.get("message", "")
                    break
                terminated = cs.get("state", {}).get("terminated", {})
                t_reason = terminated.get("reason", "")
                if t_reason in ("OOMKilled", "Error") and restart_count > 0:
                    effective_status = t_reason
                    status_reason = f"exit code {terminated.get('exitCode', '')}"
                    break
            # Check init containers too
            for cs in status.get("initContainerStatuses", []) or []:
                waiting = cs.get("state", {}).get("waiting", {})
                reason = waiting.get("reason", "")
                if reason:
                    effective_status = f"Init:{reason}"
                    status_reason = waiting.get("message", "")
                    break

            # Extract container images
            containers = spec.get("containers", [])
            init_containers = spec.get("initContainers", []) or []
            images = [c.get("image", "") for c in containers]

            pod_summary = {
                "name": metadata.get("name", ""),
                "namespace": metadata.get("namespace", ""),
                "owner_references": _owner_references_summary(metadata.get("ownerReferences", [])),
                "phase": status.get("phase", "Unknown"),
                "status": effective_status,
                "status_reason": status_reason,
                "ready": f"{ready_count}/{total_count}",
                "restarts": restart_count,
                "restart_count": restart_count,
                "node_name": spec.get("nodeName", ""),
                "image": images[0] if images else "",
                "images": images,
                "pod_ip": status.get("podIP", ""),
                "creation_timestamp": metadata.get("creationTimestamp", ""),
                "labels": metadata.get("labels", {}),
                "label_count": len(metadata.get("labels", {}) or {}),
                "service_account_name": spec.get("serviceAccountName", ""),
                "node_selector": spec.get("nodeSelector", {}) or {},
                "tolerations": [
                    {
                        "key": tol.get("key", ""),
                        "operator": tol.get("operator", ""),
                        "value": tol.get("value", ""),
                        "effect": tol.get("effect", ""),
                        "toleration_seconds": tol.get("tolerationSeconds"),
                    }
                    for tol in spec.get("tolerations", []) or []
                    if isinstance(tol, dict)
                ],
                "affinity": _affinity_summary(spec.get("affinity", {})),
                "containers": [_container_summary(c) for c in containers if isinstance(c, dict)],
                "init_containers": [_container_summary(c) for c in init_containers if isinstance(c, dict)],
                "volumes": [_volume_summary(v) for v in spec.get("volumes", []) or [] if isinstance(v, dict)],
                "container_states": container_states,
            }
            
            # Add condition information
            conditions = status.get("conditions", [])
            pod_summary["conditions"] = {
                cond.get("type"): cond.get("status") for cond in conditions if cond.get("type")
            }
            
            # Add reason if pod is not running normally
            if status.get("phase") != "Running" or restart_count > 0:
                pod_summary["reason"] = status.get("reason", "")
                pod_summary["message"] = status.get("message", "")
            
            pods.append(pod_summary)
            
        except Exception as e:
            # Log but don't fail entire parse
            import logging
            logging.getLogger(__name__).warning(f"Failed to parse pod item: {e}")
            continue
    
    return pods


def parse_deployment(json_output: dict) -> Dict[str, Any]:
    """
    Parse kubectl get deployment JSON output.
    
    Args:
        json_output: JSON dict from kubectl get deployment -o json
        
    Returns:
        Deployment summary dict with health indicators
    """
    if not isinstance(json_output, dict):
        return {"error": "Invalid JSON output"}
    
    metadata = json_output.get("metadata", {})
    spec = json_output.get("spec", {})
    status = json_output.get("status", {})
    pod_template = _pod_template_summary(spec.get("template", {}))
    
    desired = spec.get("replicas", 0)
    ready = status.get("readyReplicas", 0)
    available = status.get("availableReplicas", 0)
    unavailable = status.get("unavailableReplicas", 0)
    
    deployment_summary = {
        "name": metadata.get("name", ""),
        "namespace": metadata.get("namespace", ""),
        "labels": metadata.get("labels", {}) or {},
        "label_count": len(metadata.get("labels", {}) or {}),
        "annotations": _annotations_summary(metadata.get("annotations", {}) or {}),
        "replicas": {
            "desired": desired,
            "current": status.get("replicas", 0),
            "updated": status.get("updatedReplicas", 0),
            "ready": ready,
            "available": available,
            "unavailable": unavailable,
        },
        "selector": spec.get("selector", {}) or {},
        "strategy": spec.get("strategy", {}),
        "revision": metadata.get("annotations", {}).get("deployment.kubernetes.io/revision", ""),
        "generation": metadata.get("generation"),
        "observed_generation": status.get("observedGeneration"),
        "pod_template": pod_template,
        "conditions": [],
        "health_status": "healthy" if ready == desired and desired > 0 else "unhealthy",
        "creation_timestamp": metadata.get("creationTimestamp", ""),
    }
    
    # Parse conditions with better structure
    for cond in status.get("conditions", []):
        condition = {
            "type": cond.get("type", ""),
            "status": cond.get("status", ""),
            "reason": cond.get("reason", ""),
            "message": cond.get("message", ""),
            "last_update_time": cond.get("lastUpdateTime", ""),
            "last_transition_time": cond.get("lastTransitionTime", ""),
        }
        deployment_summary["conditions"].append(condition)
    
    # Add diagnostic hints
    if unavailable > 0:
        deployment_summary["diagnostic_hint"] = f"{unavailable} replica(s) unavailable"
    elif ready < desired:
        deployment_summary["diagnostic_hint"] = f"Only {ready}/{desired} replicas ready"
    
    return deployment_summary


def parse_service(json_output: dict) -> Dict[str, Any]:
    """
    Parse kubectl get service JSON output.
    
    Args:
        json_output: JSON dict from kubectl get service -o json
        
    Returns:
        Service summary dict with selector and port information
    """
    if not isinstance(json_output, dict):
        return {"error": "Invalid JSON output"}
    
    metadata = json_output.get("metadata", {})
    spec = json_output.get("spec", {})
    status = json_output.get("status", {})
    labels = metadata.get("labels", {}) or {}
    
    service_summary = {
        "name": metadata.get("name", ""),
        "namespace": metadata.get("namespace", ""),
        "labels": labels,
        "label_count": len(labels),
        "annotations": _annotations_summary(metadata.get("annotations", {}) or {}),
        "type": spec.get("type", "ClusterIP"),
        "cluster_ip": spec.get("clusterIP", ""),
        "cluster_ips": spec.get("clusterIPs", []) or [],
        "external_ips": spec.get("externalIPs", []),
        "external_name": spec.get("externalName", ""),
        "selector": spec.get("selector", {}),
        "ports": [],
        "load_balancer": status.get("loadBalancer", {}) or {},
        "session_affinity": spec.get("sessionAffinity", "None"),
        "external_traffic_policy": spec.get("externalTrafficPolicy", ""),
        "internal_traffic_policy": spec.get("internalTrafficPolicy", ""),
        "ip_families": spec.get("ipFamilies", []) or [],
        "ip_family_policy": spec.get("ipFamilyPolicy", ""),
        "creation_timestamp": metadata.get("creationTimestamp", ""),
    }
    
    # Parse ports
    for port in spec.get("ports", []) or []:
        if not isinstance(port, dict):
            continue
        port_info = {
            "name": port.get("name", ""),
            "protocol": port.get("protocol", "TCP"),
            "port": port.get("port"),
            "target_port": port.get("targetPort"),
            "app_protocol": port.get("appProtocol", ""),
        }
        # Only include nodePort if it exists (for NodePort/LoadBalancer services)
        if "nodePort" in port:
            port_info["node_port"] = port.get("nodePort")
        service_summary["ports"].append(port_info)
    
    # Add diagnostic hint if no selector (headless service or external service)
    if not service_summary["selector"]:
        service_summary["diagnostic_hint"] = "Service has no selector - may be headless or external"
    
    return service_summary


def parse_endpoints(json_output: dict) -> Dict[str, Any]:
    """
    Parse kubectl get endpoints JSON output.
    
    Args:
        json_output: JSON dict from kubectl get endpoints -o json
        
    Returns:
        Endpoints summary dict with diagnostic information
    """
    if not isinstance(json_output, dict):
        return {"error": "Invalid JSON output"}
    
    metadata = json_output.get("metadata", {})
    subsets = json_output.get("subsets", [])
    
    addresses = []
    not_ready_addresses = []
    ports = []
    
    for subset in subsets:
        # Parse ready addresses
        for addr in subset.get("addresses", []):
            target_ref = addr.get("targetRef", {})
            addresses.append({
                "ip": addr.get("ip", ""),
                "hostname": addr.get("hostname", ""),
                "node_name": addr.get("nodeName", ""),
                "target_ref": {
                    "kind": target_ref.get("kind", ""),
                    "name": target_ref.get("name", ""),
                    "namespace": target_ref.get("namespace", ""),
                },
            })
        
        # Parse not-ready addresses
        for addr in subset.get("notReadyAddresses", []):
            target_ref = addr.get("targetRef", {})
            not_ready_addresses.append({
                "ip": addr.get("ip", ""),
                "hostname": addr.get("hostname", ""),
                "node_name": addr.get("nodeName", ""),
                "target_ref": {
                    "kind": target_ref.get("kind", ""),
                    "name": target_ref.get("name", ""),
                    "namespace": target_ref.get("namespace", ""),
                },
            })
        
        # Parse ports
        for port in subset.get("ports", []):
            ports.append({
                "name": port.get("name", ""),
                "port": port.get("port", 0),
                "protocol": port.get("protocol", "TCP"),
            })
    
    ready_count = len(addresses)
    not_ready_count = len(not_ready_addresses)
    
    result = {
        "name": metadata.get("name", ""),
        "namespace": metadata.get("namespace", ""),
        "ready_addresses": addresses,
        "not_ready_addresses": not_ready_addresses,
        "ports": ports,
        "ready_count": ready_count,
        "not_ready_count": not_ready_count,
        "has_endpoints": ready_count > 0,
    }
    
    # Add diagnostic hint
    if ready_count == 0 and not_ready_count == 0:
        result["diagnostic_hint"] = "No endpoints found - check if pods match service selector"
    elif ready_count == 0 and not_ready_count > 0:
        result["diagnostic_hint"] = f"{not_ready_count} endpoint(s) not ready - check pod readiness"
    
    return result


def parse_endpoint_slices(json_output: dict) -> Dict[str, Any]:
    """
    Parse kubectl get endpointslices JSON output into connectivity evidence.

    EndpointSlice includes readiness dimensions that legacy Endpoints cannot
    represent, especially serving and terminating state.
    """
    if not isinstance(json_output, dict):
        return {"error": "Invalid JSON output"}

    slices = []
    endpoints = []
    ports_by_key: dict[tuple, dict] = {}

    for item in json_output.get("items", []) or []:
        metadata = item.get("metadata", {}) or {}
        address_type = item.get("addressType", "")
        slice_ports = []
        for port in item.get("ports", []) or []:
            if not isinstance(port, dict):
                continue
            port_info = {
                "name": port.get("name", ""),
                "port": port.get("port"),
                "protocol": port.get("protocol", "TCP"),
                "app_protocol": port.get("appProtocol", ""),
            }
            slice_ports.append(port_info)
            ports_by_key[(
                port_info["name"],
                port_info["port"],
                port_info["protocol"],
                port_info["app_protocol"],
            )] = port_info

        slice_endpoint_count = 0
        for endpoint in item.get("endpoints", []) or []:
            if not isinstance(endpoint, dict):
                continue
            target_ref = endpoint.get("targetRef", {}) or {}
            conditions = endpoint.get("conditions", {}) or {}
            hints = endpoint.get("hints", {}) or {}
            zones = [
                zone.get("name", "")
                for zone in hints.get("forZones", []) or []
                if isinstance(zone, dict) and zone.get("name")
            ]
            endpoint_info = {
                "slice_name": metadata.get("name", ""),
                "addresses": endpoint.get("addresses", []) or [],
                "address_type": address_type,
                "hostname": endpoint.get("hostname", ""),
                "node_name": endpoint.get("nodeName", ""),
                "zone": endpoint.get("zone", ""),
                "hints_for_zones": zones,
                "conditions": {
                    "ready": conditions.get("ready"),
                    "serving": conditions.get("serving"),
                    "terminating": conditions.get("terminating"),
                },
                "target_ref": {
                    "kind": target_ref.get("kind", ""),
                    "name": target_ref.get("name", ""),
                    "namespace": target_ref.get("namespace", ""),
                },
                "ports": slice_ports,
            }
            endpoints.append(endpoint_info)
            slice_endpoint_count += 1

        slices.append({
            "name": metadata.get("name", ""),
            "namespace": metadata.get("namespace", ""),
            "labels": metadata.get("labels", {}) or {},
            "address_type": address_type,
            "ports": slice_ports,
            "endpoint_count": slice_endpoint_count,
        })

    ready_count = sum(1 for endpoint in endpoints if endpoint.get("conditions", {}).get("ready") is True)
    not_ready_count = sum(1 for endpoint in endpoints if endpoint.get("conditions", {}).get("ready") is False)
    serving_count = sum(1 for endpoint in endpoints if endpoint.get("conditions", {}).get("serving") is True)
    terminating_count = sum(1 for endpoint in endpoints if endpoint.get("conditions", {}).get("terminating") is True)

    result = {
        "slice_count": len(slices),
        "endpoint_count": len(endpoints),
        "ready_count": ready_count,
        "not_ready_count": not_ready_count,
        "serving_count": serving_count,
        "terminating_count": terminating_count,
        "ports": list(ports_by_key.values()),
        "slices": slices,
        "endpoints": endpoints,
        "has_endpoints": ready_count > 0,
    }

    if not endpoints:
        result["diagnostic_hint"] = "No EndpointSlice endpoints found - check service selector and backing pods"
    elif ready_count == 0:
        result["diagnostic_hint"] = "EndpointSlice endpoints exist, but none are ready"
    elif terminating_count > 0:
        result["diagnostic_hint"] = f"{terminating_count} endpoint(s) are terminating"

    return result


def parse_events(json_output: dict) -> List[Dict[str, Any]]:
    """
    Parse kubectl get events JSON output.
    
    Args:
        json_output: JSON dict from kubectl get events -o json
        
    Returns:
        List of event summary dicts, sorted by timestamp (most recent first)
    """
    if not isinstance(json_output, dict):
        return []
    
    events = []
    
    for item in json_output.get("items", []):
        try:
            metadata = item.get("metadata", {})
            
            # SAFETY: Truncate long messages
            message = item.get("message", "")
            if len(message) > 500:
                message = message[:500] + "... [truncated]"
            
            # Resolve timestamp — newer K8s uses eventTime, older uses lastTimestamp
            last_ts = item.get("lastTimestamp") or item.get("eventTime") or metadata.get("creationTimestamp") or ""
            first_ts = item.get("firstTimestamp") or item.get("eventTime") or metadata.get("creationTimestamp") or ""
            
            event_summary = {
                "type": item.get("type", "Normal"),
                "reason": item.get("reason", ""),
                "message": message,
                "count": item.get("count", 1),
                "first_timestamp": first_ts,
                "last_timestamp": last_ts,
                "involved_object": {
                    "kind": item.get("involvedObject", {}).get("kind", ""),
                    "name": item.get("involvedObject", {}).get("name", ""),
                    "namespace": item.get("involvedObject", {}).get("namespace", ""),
                },
                "source": item.get("source", {}).get("component", "") if isinstance(item.get("source"), dict) else item.get("reportingComponent", ""),
            }
            
            events.append(event_summary)
        except Exception as e:
            # Log but don't fail entire parse
            import logging
            logging.getLogger(__name__).warning(f"Failed to parse event item: {e}")
            continue
    
    # Sort by last timestamp (most recent first) — guard against None values
    events.sort(
        key=lambda e: e.get("last_timestamp") or "",
        reverse=True
    )
    
    return events


def parse_pod_describe_highlights(describe_text: str) -> Dict[str, Any]:
    """
    Extract key information from kubectl describe pod output.
    
    Args:
        describe_text: Raw text from kubectl describe pod
        
    Returns:
        Dict with highlighted information
    """
    highlights = {
        "restart_count": 0,
        "state": "Unknown",
        "last_state": None,
        "ready": "Unknown",
        "conditions": [],
        "warnings": [],
    }
    
    lines = describe_text.split("\n")
    
    for i, line in enumerate(lines):
        # Extract restart count
        if "Restart Count:" in line:
            match = re.search(r'Restart Count:\s*(\d+)', line)
            if match:
                highlights["restart_count"] = int(match.group(1))
        
        # Extract state
        if "State:" in line and i + 1 < len(lines):
            state_line = lines[i + 1].strip()
            highlights["state"] = state_line
        
        # Extract last state
        if "Last State:" in line and i + 1 < len(lines):
            last_state_line = lines[i + 1].strip()
            if last_state_line and last_state_line != "Terminated":
                highlights["last_state"] = last_state_line
        
        # Extract ready status
        if "Ready:" in line:
            match = re.search(r'Ready:\s*(\w+)', line)
            if match:
                highlights["ready"] = match.group(1)
        
        # Extract conditions
        if "Conditions:" in line:
            j = i + 1
            while j < len(lines) and lines[j].startswith("  "):
                cond_line = lines[j].strip()
                if cond_line:
                    highlights["conditions"].append(cond_line)
                j += 1
        
        # Look for warning events
        if "Warning" in line or "Error" in line or "Failed" in line:
            highlights["warnings"].append(line.strip())
    
    return highlights


def truncate_logs(log_text: str, max_lines: int) -> tuple[str, bool]:
    """
    Truncate log text to maximum number of lines.
    
    Args:
        log_text: Raw log text
        max_lines: Maximum number of lines to keep
        
    Returns:
        Tuple of (truncated_text, was_truncated)
    """
    lines = log_text.split("\n")
    
    if len(lines) <= max_lines:
        return log_text, False
    
    # Keep last N lines (most recent)
    truncated_lines = lines[-max_lines:]
    truncated_text = "\n".join(truncated_lines)
    truncated_text = f"[... showing last {max_lines} lines ...]\n" + truncated_text
    
    return truncated_text, True
