"""Deterministic diagnostic rules for Kubernetes health analysis."""

import logging
from typing import Any, Dict, List, Optional
from .models import EvidenceRef, Finding, Severity

logger = logging.getLogger(__name__)


def check_pod_rules(pods: List[Dict[str, Any]], events: List[Dict[str, Any]], restart_threshold: int = 5) -> List[Finding]:
    """Evaluate deterministic rules over pod specifications and status."""
    findings: List[Finding] = []

    # Map events by pod name for easy correlation
    pod_events: Dict[str, List[Dict[str, Any]]] = {}
    for ev in events:
        obj = ev.get("involvedObject", {})
        if obj.get("kind") == "Pod" and obj.get("name"):
            pod_name = obj["name"]
            pod_events.setdefault(pod_name, []).append(ev)

    for pod in pods:
        metadata = pod.get("metadata", {})
        pod_name = metadata.get("name", "unknown")
        namespace = metadata.get("namespace", "default")
        status = pod.get("status", {})
        spec = pod.get("spec", {})

        container_statuses = status.get("containerStatuses", []) + status.get("initContainerStatuses", [])
        
        # Correlated warning event messages for this pod
        recent_events = pod_events.get(pod_name, [])
        warning_messages = [
            f"{e.get('reason', 'Event')}: {e.get('message', '')}"
            for e in recent_events
            if e.get("type") == "Warning"
        ]

        for cs in container_statuses:
            c_name = cs.get("name", "container")
            restart_count = cs.get("restartCount", 0)
            state = cs.get("state", {})
            last_state = cs.get("lastState", {})

            # 1. CrashLoopBackOff
            waiting = state.get("waiting", {})
            reason = waiting.get("reason", "")
            if reason == "CrashLoopBackOff":
                evidence = [
                    EvidenceRef(
                        source="pod_status",
                        namespace=namespace,
                        kind="Pod",
                        name=pod_name,
                        field=f"status.containerStatuses[{c_name}].state.waiting.reason",
                        value=reason,
                        message=waiting.get("message", f"Container {c_name} is crash looping"),
                    )
                ]
                for msg in warning_messages[:3]:
                    evidence.append(
                        EvidenceRef(
                            source="event",
                            namespace=namespace,
                            kind="Pod",
                            name=pod_name,
                            message=msg,
                        )
                    )

                findings.append(
                    Finding(
                        rule_id="pod_waiting_crashloopbackoff",
                        severity=Severity.CRITICAL.value,
                        category="pod",
                        title=f"Container '{c_name}' in Pod '{pod_name}' is CrashLoopBackOff",
                        summary=f"Container '{c_name}' in pod '{pod_name}' ({namespace}) failed to start or crashed repeatedly (restarts: {restart_count}).",
                        namespace=namespace,
                        resource_kind="Pod",
                        resource_name=pod_name,
                        evidence=evidence,
                        probable_cause="Application crash, missing required configuration, or failed initialization script.",
                        recommended_next_steps=[
                            f"Check logs for container '{c_name}': kubectl logs {pod_name} -c {c_name} -n {namespace}",
                            f"Check previous logs: kubectl logs {pod_name} -c {c_name} -n {namespace} --previous",
                        ],
                        suggested_read_only_commands=[
                            f"kubectl logs {pod_name} -c {c_name} -n {namespace} --tail=100",
                            f"kubectl describe pod {pod_name} -n {namespace}",
                        ],
                    )
                )

            # 2. ImagePullBackOff / ErrImagePull
            elif reason in ("ImagePullBackOff", "ErrImagePull"):
                evidence = [
                    EvidenceRef(
                        source="pod_status",
                        namespace=namespace,
                        kind="Pod",
                        name=pod_name,
                        field=f"status.containerStatuses[{c_name}].state.waiting.reason",
                        value=reason,
                        message=waiting.get("message"),
                    )
                ]
                for msg in warning_messages[:3]:
                    evidence.append(
                        EvidenceRef(
                            source="event",
                            namespace=namespace,
                            kind="Pod",
                            name=pod_name,
                            message=msg,
                        )
                    )

                findings.append(
                    Finding(
                        rule_id="pod_waiting_image_pull",
                        severity=Severity.CRITICAL.value,
                        category="pod",
                        title=f"Image pull failure for Container '{c_name}' in Pod '{pod_name}'",
                        summary=f"Container '{c_name}' cannot pull image due to state '{reason}'.",
                        namespace=namespace,
                        resource_kind="Pod",
                        resource_name=pod_name,
                        evidence=evidence,
                        probable_cause="Invalid image tag, repository URL error, missing imagePullSecrets, or registry auth failure.",
                        recommended_next_steps=[
                            f"Verify container image tag in spec.",
                            f"Check image pull secrets in namespace {namespace}.",
                        ],
                        suggested_read_only_commands=[
                            f"kubectl get pod {pod_name} -n {namespace} -o jsonpath='{{.spec.containers[*].image}}'",
                            f"kubectl describe pod {pod_name} -n {namespace}",
                        ],
                    )
                )

            # 3. CreateContainerConfigError / CreateContainerError
            elif reason in ("CreateContainerConfigError", "CreateContainerError"):
                evidence = [
                    EvidenceRef(
                        source="pod_status",
                        namespace=namespace,
                        kind="Pod",
                        name=pod_name,
                        field=f"status.containerStatuses[{c_name}].state.waiting.reason",
                        value=reason,
                        message=waiting.get("message"),
                    )
                ]
                for msg in warning_messages[:3]:
                    evidence.append(
                        EvidenceRef(
                            source="event",
                            namespace=namespace,
                            kind="Pod",
                            name=pod_name,
                            message=msg,
                        )
                    )

                findings.append(
                    Finding(
                        rule_id="pod_create_container_config_error",
                        severity=Severity.CRITICAL.value,
                        category="pod",
                        title=f"Configuration error for Container '{c_name}' in Pod '{pod_name}'",
                        summary=f"Container '{c_name}' failed to create due to configuration error '{reason}'.",
                        namespace=namespace,
                        resource_kind="Pod",
                        resource_name=pod_name,
                        evidence=evidence,
                        probable_cause="Referenced ConfigMap or Secret is missing, or env key reference is invalid.",
                        recommended_next_steps=[
                            f"Inspect warning events in pod describe for missing Secret/ConfigMap keys.",
                        ],
                        suggested_read_only_commands=[
                            f"kubectl describe pod {pod_name} -n {namespace}",
                        ],
                    )
                )

            # 4. OOMKilled
            terminated = last_state.get("terminated", {}) or state.get("terminated", {})
            term_reason = terminated.get("reason", "")
            if term_reason == "OOMKilled":
                evidence = [
                    EvidenceRef(
                        source="pod_status",
                        namespace=namespace,
                        kind="Pod",
                        name=pod_name,
                        field=f"status.containerStatuses[{c_name}].lastState.terminated.reason",
                        value="OOMKilled",
                        message=f"Exit code {terminated.get('exitCode', 137)}",
                    )
                ]
                findings.append(
                    Finding(
                        rule_id="pod_oomkilled",
                        severity=Severity.CRITICAL.value,
                        category="pod",
                        title=f"Container '{c_name}' in Pod '{pod_name}' was OOMKilled",
                        summary=f"Container '{c_name}' exceeded its memory limit and was killed by the kernel OOM killer.",
                        namespace=namespace,
                        resource_kind="Pod",
                        resource_name=pod_name,
                        evidence=evidence,
                        probable_cause="Memory limit too low or application memory leak.",
                        recommended_next_steps=[
                            f"Increase memory limits for container '{c_name}'.",
                            f"Analyze memory metrics and heap dumps.",
                        ],
                        suggested_read_only_commands=[
                            f"kubectl get pod {pod_name} -n {namespace} -o jsonpath='{{.spec.containers[?(@.name==\"{c_name}\")].resources}}'",
                        ],
                    )
                )

            # 5. High restart count warning
            if restart_count >= restart_threshold and reason not in ("CrashLoopBackOff", "OOMKilled"):
                evidence = [
                    EvidenceRef(
                        source="pod_status",
                        namespace=namespace,
                        kind="Pod",
                        name=pod_name,
                        field=f"status.containerStatuses[{c_name}].restartCount",
                        value=str(restart_count),
                    )
                ]
                findings.append(
                    Finding(
                        rule_id="pod_high_restart_count",
                        severity=Severity.WARNING.value,
                        category="pod",
                        title=f"High restart count ({restart_count}) for Container '{c_name}' in Pod '{pod_name}'",
                        summary=f"Container '{c_name}' in pod '{pod_name}' has restarted {restart_count} times.",
                        namespace=namespace,
                        resource_kind="Pod",
                        resource_name=pod_name,
                        evidence=evidence,
                        probable_cause="Intermittent Liveness probe failures or transient crashes.",
                        recommended_next_steps=[
                            f"Check liveness and readiness probe configurations.",
                        ],
                        suggested_read_only_commands=[
                            f"kubectl describe pod {pod_name} -n {namespace}",
                        ],
                    )
                )

    return findings


def check_scheduling_and_node_rules(
    pods: List[Dict[str, Any]],
    nodes: List[Dict[str, Any]],
    events: List[Dict[str, Any]]
) -> List[Finding]:
    """Evaluate rules for unschedulable pending pods and node pressure conditions."""
    findings: List[Finding] = []

    # 1. Pending pods with FailedScheduling events
    for pod in pods:
        status = pod.get("status", {})
        phase = status.get("phase", "")
        if phase == "Pending":
            metadata = pod.get("metadata", {})
            pod_name = metadata.get("name", "unknown")
            namespace = metadata.get("namespace", "default")

            sched_events = [
                e.get("message", "")
                for e in events
                if e.get("involvedObject", {}).get("name") == pod_name
                and e.get("reason") in ("FailedScheduling", "FailedBinding")
            ]

            if sched_events:
                evidence = [
                    EvidenceRef(
                        source="pod_status",
                        namespace=namespace,
                        kind="Pod",
                        name=pod_name,
                        field="status.phase",
                        value="Pending",
                    )
                ]
                for msg in sched_events[:3]:
                    evidence.append(
                        EvidenceRef(
                            source="event",
                            namespace=namespace,
                            kind="Pod",
                            name=pod_name,
                            message=msg,
                        )
                    )

                findings.append(
                    Finding(
                        rule_id="pod_pending_failed_scheduling",
                        severity=Severity.WARNING.value,
                        category="scheduling",
                        title=f"Pod '{pod_name}' is Pending due to scheduling failures",
                        summary=f"Pod '{pod_name}' ({namespace}) cannot be scheduled onto any node.",
                        namespace=namespace,
                        resource_kind="Pod",
                        resource_name=pod_name,
                        evidence=evidence,
                        probable_cause="Insufficient CPU/Memory on nodes, node taints/tolerations mismatch, or unsatisfied node affinity.",
                        recommended_next_steps=[
                            "Inspect node capacity and resource request thresholds.",
                            "Check pod taints, tolerations, and node selectors.",
                        ],
                        suggested_read_only_commands=[
                            f"kubectl describe pod {pod_name} -n {namespace}",
                            "kubectl get nodes -o wide",
                        ],
                    )
                )

    # 2. Node conditions
    for node in nodes:
        metadata = node.get("metadata", {})
        node_name = metadata.get("name", "unknown")
        status = node.get("status", {})
        conditions = status.get("conditions", [])

        ready_cond = next((c for c in conditions if c.get("type") == "Ready"), None)
        if ready_cond and ready_cond.get("status") != "True":
            evidence = [
                EvidenceRef(
                    source="node_condition",
                    kind="Node",
                    name=node_name,
                    field="status.conditions[Ready]",
                    value=ready_cond.get("status"),
                    message=ready_cond.get("message"),
                )
            ]
            findings.append(
                Finding(
                    rule_id="node_not_ready",
                    severity=Severity.CRITICAL.value,
                    category="node",
                    title=f"Node '{node_name}' is NotReady",
                    summary=f"Kubernetes node '{node_name}' is in NotReady state (status: {ready_cond.get('status')}).",
                    resource_kind="Node",
                    resource_name=node_name,
                    evidence=evidence,
                    probable_cause="Kubelet service down, CNI plugin network failure, or host network issue.",
                    recommended_next_steps=[
                        f"Check kubelet logs on host '{node_name}'.",
                        f"Check node network interface status.",
                    ],
                    suggested_read_only_commands=[
                        f"kubectl describe node {node_name}",
                    ],
                )
            )

        # Pressure conditions
        for c in conditions:
            c_type = c.get("type")
            if c_type in ("MemoryPressure", "DiskPressure", "PIDPressure") and c.get("status") == "True":
                evidence = [
                    EvidenceRef(
                        source="node_condition",
                        kind="Node",
                        name=node_name,
                        field=f"status.conditions[{c_type}]",
                        value="True",
                        message=c.get("message"),
                    )
                ]
                findings.append(
                    Finding(
                        rule_id="node_pressure",
                        severity=Severity.WARNING.value,
                        category="node",
                        title=f"Node '{node_name}' has condition '{c_type}'",
                        summary=f"Node '{node_name}' is experiencing resource pressure: {c_type}.",
                        resource_kind="Node",
                        resource_name=node_name,
                        evidence=evidence,
                        probable_cause=f"High {c_type.lower().replace('pressure', '')} consumption on node host.",
                        recommended_next_steps=[
                            f"Free up resources on node '{node_name}' or drain/reschedule workloads.",
                        ],
                        suggested_read_only_commands=[
                            f"kubectl describe node {node_name}",
                            f"kubectl top node {node_name}",
                        ],
                    )
                )

    return findings


def check_service_and_rollout_rules(
    services: List[Dict[str, Any]],
    endpoints: List[Dict[str, Any]],
    deployments: List[Dict[str, Any]],
    pvcs: List[Dict[str, Any]]
) -> List[Finding]:
    """Evaluate rules for services with missing endpoints, stuck rollouts, and pending PVCs."""
    findings: List[Finding] = []

    # Map endpoints by (namespace, service_name)
    ep_map: Dict[tuple, Dict[str, Any]] = {}
    for ep in endpoints:
        meta = ep.get("metadata", {})
        ep_map[(meta.get("namespace", "default"), meta.get("name", ""))] = ep

    # 1. Service with no endpoints
    for svc in services:
        meta = svc.get("metadata", {})
        svc_name = meta.get("name", "unknown")
        namespace = meta.get("namespace", "default")
        spec = svc.get("spec", {})

        # Ignore ExternalName services
        if spec.get("type") == "ExternalName" or not spec.get("selector"):
            continue

        ep = ep_map.get((namespace, svc_name))
        has_ready_address = False

        if ep:
            subsets = ep.get("subsets", [])
            for s in subsets:
                if s.get("addresses"):
                    has_ready_address = True
                    break

        if not has_ready_address:
            evidence = [
                EvidenceRef(
                    source="service",
                    namespace=namespace,
                    kind="Service",
                    name=svc_name,
                    field="spec.selector",
                    value=str(spec.get("selector")),
                    message="Zero ready endpoints found for service",
                )
            ]
            findings.append(
                Finding(
                    rule_id="service_no_endpoints",
                    severity=Severity.WARNING.value,
                    category="service",
                    title=f"Service '{svc_name}' has zero ready endpoints",
                    summary=f"Service '{svc_name}' ({namespace}) has no ready target pod endpoints.",
                    namespace=namespace,
                    resource_kind="Service",
                    resource_name=svc_name,
                    evidence=evidence,
                    probable_cause="Service selector does not match any pod labels, or backing pods are failing readiness probes.",
                    recommended_next_steps=[
                        f"Verify service selector: {spec.get('selector')}.",
                        f"Check if pods matching selector exist and are in Ready state.",
                    ],
                    suggested_read_only_commands=[
                        f"kubectl get endpoints {svc_name} -n {namespace}",
                        f"kubectl get pods -n {namespace} --show-labels",
                    ],
                )
            )

    # 2. Deployment rollout progress / unavailable replicas
    for dep in deployments:
        meta = dep.get("metadata", {})
        dep_name = meta.get("name", "unknown")
        namespace = meta.get("namespace", "default")
        status = dep.get("status", {})

        conditions = status.get("conditions", [])
        progress_cond = next((c for c in conditions if c.get("type") == "Progressing"), None)

        if progress_cond and progress_cond.get("reason") == "ProgressDeadlineExceeded":
            evidence = [
                EvidenceRef(
                    source="rollout",
                    namespace=namespace,
                    kind="Deployment",
                    name=dep_name,
                    field="status.conditions[Progressing].reason",
                    value="ProgressDeadlineExceeded",
                    message=progress_cond.get("message"),
                )
            ]
            findings.append(
                Finding(
                    rule_id="rollout_progress_deadline_exceeded",
                    severity=Severity.CRITICAL.value,
                    category="rollout",
                    title=f"Deployment '{dep_name}' rollout deadline exceeded",
                    summary=f"Deployment '{dep_name}' ({namespace}) failed to complete rollout within deadline.",
                    namespace=namespace,
                    resource_kind="Deployment",
                    resource_name=dep_name,
                    evidence=evidence,
                    probable_cause="New replica set pods are failing to become ready (e.g. CrashLoopBackOff or probe failure).",
                    recommended_next_steps=[
                        f"Check status of newly spawned ReplicaSet pods.",
                        f"Consider rolling back deployment: kubectl rollout undo deployment/{dep_name} -n {namespace}",
                    ],
                    suggested_read_only_commands=[
                        f"kubectl rollout status deployment/{dep_name} -n {namespace}",
                        f"kubectl get pods -n {namespace} -l app={dep_name}",
                    ],
                )
            )

    # 3. Pending PVCs
    for pvc in pvcs:
        meta = pvc.get("metadata", {})
        pvc_name = meta.get("name", "unknown")
        namespace = meta.get("namespace", "default")
        status = pvc.get("status", {})
        phase = status.get("phase", "")

        if phase == "Pending":
            evidence = [
                EvidenceRef(
                    source="pvc",
                    namespace=namespace,
                    kind="PersistentVolumeClaim",
                    name=pvc_name,
                    field="status.phase",
                    value="Pending",
                )
            ]
            findings.append(
                Finding(
                    rule_id="pvc_pending",
                    severity=Severity.WARNING.value,
                    category="storage",
                    title=f"PVC '{pvc_name}' is stuck Pending",
                    summary=f"PersistentVolumeClaim '{pvc_name}' ({namespace}) is in Pending state.",
                    namespace=namespace,
                    resource_kind="PersistentVolumeClaim",
                    resource_name=pvc_name,
                    evidence=evidence,
                    probable_cause="No matching PersistentVolume available, StorageClass provisioner failure, or volume binding mode restriction.",
                    recommended_next_steps=[
                        f"Check StorageClass definition and dynamic provisioner logs.",
                    ],
                    suggested_read_only_commands=[
                        f"kubectl describe pvc {pvc_name} -n {namespace}",
                        "kubectl get storageclass",
                    ],
                )
            )

    return findings
