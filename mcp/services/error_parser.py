import re
import hashlib
from typing import Any

# ── Kubernetes error categories ───────────────────────────────────────────────
K8S_PATTERNS = {
    "pod_crashloop": [
        r"CrashLoopBackOff",
        r"Back-off restarting failed container",
        r"container.*crash",
    ],
    "pod_oom": [
        r"OOMKilled",
        r"OutOfMemory",
        r"memory limit exceeded",
        r"Killed.*memory",
    ],
    "pod_image": [
        r"ImagePullBackOff",
        r"ErrImagePull",
        r"Failed to pull image",
        r"Back-off pulling image",
        r"manifest.*not found",
        r"manifest unknown",
        r"no matching manifest",
        r"failed to resolve reference",
        r"pull access denied",
        r"unauthorized.*repository",
        r"image.*not found",
    ],
    "pod_pending": [
        r"Pending.*Unschedulable",
        r"0/\d+ nodes are available",
        r"Insufficient (cpu|memory|pods)",
        r"No nodes.*match.*node ?[Ss]elector",
        r"node\(s\) didn't match",
        r"Unschedulable",
    ],
    "pod_evicted": [
        r"Evicted",
        r"The node was low on resource",
        r"eviction.*threshold",
        r"disk.*pressure",
        r"memory.*pressure",
    ],
    "pod_init_error": [
        r"Init:Error",
        r"Init:CrashLoopBackOff",
        r"init container.*failed",
        r"initContainers.*Error",
    ],
    "container_config": [
        r"invalid.*environment variable",
        r"cannot unmarshal.*into Go struct",
        r"json:.*cannot unmarshal",
        r"unknown field",
        r"spec.*invalid",
    ],
    "rbac": [
        r"forbidden.*User.*cannot",
        r"RBAC.*denied",
        r"is forbidden",
        r"does not have.*permission",
        r"Unauthorized",
        r"no.*RBAC policy",
    ],
    "networking": [
        r"connection refused",
        r"dial.*timeout",
        r"i/o timeout",
        r"no route to host",
        r"EOF.*connection",
        r"network.*unreachable",
        r"failed to connect",
        r"Service.*ClusterIP.*unreachable",
    ],
    "storage": [
        r"persistentvolumeclaim.*not found",
        r"no persistent volumes available",
        r"FailedMount",
        r"Unable to mount",
        r"volume.*not found",
        r"storageclass.*not found",
        r"ReadOnlyFileSystem",
    ],
    "resource_quota": [
        r"exceeded quota",
        r"resource quota",
        r"LimitRange",
        r"maximum allowed.*exceeded",
        r"pods.*exceeded.*quota",
    ],
    "deployment_stuck": [
        r"Deployment.*does not have minimum availability",
        r"Rollout.*stalled",
        r"ProgressDeadlineExceeded",
        r"ReplicaSet.*failed",
        r"unavailable replicas",
    ],
    "statefulset": [
        r"StatefulSet.*cannot be handled",
        r"statefulset.*invalid",
        r"StatefulSet.*failed",
        r"pod.*StatefulSet.*not ready",
    ],
    "ingress": [
        r"ingress.*not.*found",
        r"failed to create.*ingress",
        r"backend.*not.*available",
        r"TLS.*certificate.*error",
        r"ingress controller.*error",
    ],
    "api_server": [
        r"Unable to connect to the server",
        r"connection refused.*6443",
        r"the server is currently unable",
        r"etcd.*cluster.*unhealthy",
        r"apiserver.*not ready",
    ],
    "helm_error": [
        r"Failure when executing Helm command",
        r"UPGRADE FAILED",
        r"helm.*Exited [1-9]",
        r"Error:.*errors? occurred",
        r"coalesce.*Not a table",
        r"Release.*does not exist",
    ],
    "configmap_secret": [
        r"configmap.*not found",
        r"secret.*not found",
        r"failed to fetch.*configmap",
        r"referenced.*secret.*not exist",
    ],
    "node": [
        r"node.*NotReady",
        r"node.*taint",
        r"node.*cordoned",
        r"kubelet.*not.*running",
        r"node.*unreachable",
    ],
}

# ── Ansible error categories ──────────────────────────────────────────────────
ANSIBLE_PATTERNS = {
    "connection": [
        r"Failed to connect.*ssh",
        r"UNREACHABLE",
        r"Connection refused",
        r"Connection timed out",
        r"ssh.*timed out",
    ],
    "variables": [
        r"undefined variable",
        r"AnsibleUndefinedVariable",
        r"is not defined",
        r"variable.*not found",
    ],
    "syntax": [
        r"Syntax Error.*YAML",
        r"YAML.*error",
        r"parsing error",
        r"is not a valid attribute",
    ],
    "dependencies": [
        r"Failed to import",
        r"No module named",
        r"ModuleNotFoundError",
        r"could not import",
    ],
    "ssh_verification": [
        r"authenticity of host.*can't be established",
        r"Host key verification",
        r"REMOTE HOST IDENTIFICATION HAS CHANGED",
    ],
    "sudo": [
        r"privilege escalation",
        r"sudo.*password",
        r"become.*failed",
        r"Timeout.*privilege escalation",
    ],
    "permissions": [r"Permission denied", r"access denied", r"not permitted"],
    "helm_type_error": [
        r"cannot unmarshal \w+ into Go struct field",
        r"json:.*cannot unmarshal",
    ],
    "helm_install": [
        r"Failure when executing Helm command",
        r"helm.*Exited [1-9]",
    ],
    "task_failure": [r"fatal:.*FAILED", r"FAILED!", r"failed=[1-9]"],
}

KNOWN_CATEGORIES = frozenset(
    {
        "general_failure",
        "deployment_timeout_generic",
        *K8S_PATTERNS.keys(),
        *ANSIBLE_PATTERNS.keys(),
    }
)


def classify_error(error_text: str, tool: str) -> str:
    patterns = K8S_PATTERNS if tool == "kubernetes" else ANSIBLE_PATTERNS
    for category, regexes in patterns.items():
        for pattern in regexes:
            if re.search(pattern, error_text, re.IGNORECASE):
                return category
    return "general_failure"


def reconcile_category(deterministic_category: str, llm_category: str | None) -> tuple[str, str]:
    """Return the trusted category and its source.

    Any concrete deterministic match wins. The LLM may classify only when the
    deterministic parser has no specific match, and only into a known category.
    """
    if deterministic_category != "general_failure":
        return deterministic_category, "deterministic"
    normalized_llm = str(llm_category or "").strip().lower()
    if normalized_llm in KNOWN_CATEGORIES and normalized_llm != "general_failure":
        return normalized_llm, "llm"
    return "general_failure", "deterministic"


def extract_structured_k8s_context(payload: Any) -> dict:
    """Extract bounded, caller-supplied Kubernetes evidence from Ansible data.

    ``kubernetes.core.k8s`` commonly wraps the returned resource under
    ``result`` while keeping the generic timeout in ``msg``. Walk the payload
    structurally so image names and status reasons remain distinct evidence
    instead of being flattened into an ambiguous error string.
    """
    images: list[str] = []
    condition_reasons: list[str] = []
    condition_messages: list[str] = []
    ansible_messages: list[str] = []
    resource: dict[str, str] = {}

    def _add(items: list[str], value: Any, *, limit: int = 20) -> None:
        text = str(value or "").strip()
        if text and text not in items and len(items) < limit:
            items.append(text[:2000])

    def _walk(value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            kind = value.get("kind")
            metadata = value.get("metadata")
            if isinstance(kind, str) and isinstance(metadata, dict):
                resource.setdefault("kind", kind[:128])
                for field in ("name", "namespace"):
                    item = metadata.get(field)
                    if isinstance(item, str) and item:
                        resource.setdefault(field, item[:253])

            conditions = value.get("conditions")
            if isinstance(conditions, list) and (
                "status" in path or path[-1:] == ("status",)
            ):
                for condition in conditions:
                    if not isinstance(condition, dict):
                        continue
                    _add(condition_reasons, condition.get("reason"))
                    _add(condition_messages, condition.get("message"))

            for key, item in value.items():
                next_path = path + (str(key),)
                if key == "image" and any(
                    segment in {"containers", "initContainers", "ephemeralContainers"}
                    for segment in path
                ):
                    _add(images, item)
                elif key == "msg" and isinstance(item, str):
                    _add(ansible_messages, item)
                elif key == "reason" and any(
                    segment in {"conditions", "waiting", "containerStatuses"}
                    for segment in path
                ):
                    _add(condition_reasons, item)
                elif key == "message" and any(
                    segment in {"conditions", "waiting", "containerStatuses"}
                    for segment in path
                ):
                    _add(condition_messages, item)
                _walk(item, next_path)
        elif isinstance(value, list):
            for item in value:
                _walk(item, path)

    _walk(payload)
    out: dict[str, Any] = {}
    if images:
        out["images"] = images
    if condition_reasons:
        out["condition_reasons"] = condition_reasons
    if condition_messages:
        out["condition_messages"] = condition_messages
    if ansible_messages:
        out["ansible_messages"] = ansible_messages
    if resource:
        out["resource"] = resource
    return out


def _structured_category(structured: dict[str, Any]) -> tuple[str, str] | None:
    signals = [
        *structured.get("condition_reasons", []),
        *structured.get("condition_messages", []),
    ]
    if signals:
        category = classify_error("\n".join(signals), "kubernetes")
        if category != "general_failure":
            return category, "structured"

    timeout_text = "\n".join(structured.get("ansible_messages", []))
    timeout_signal = re.search(
        r"(timed?\s*out|timeout|failed to become ready|waiting for.*condition)",
        timeout_text,
        re.IGNORECASE,
    )
    has_workload_evidence = bool(
        structured.get("resource")
        or structured.get("condition_reasons")
        or structured.get("images")
    )
    if timeout_signal and has_workload_evidence:
        return "deployment_timeout_generic", "structured_fallback"
    return None


def extract_context(
    error_text: str,
    tool: str,
    structured_payload: Any = None,
) -> dict:
    category = classify_error(error_text, tool)
    category_source = "deterministic"
    structured = (
        extract_structured_k8s_context(structured_payload)
        if tool == "kubernetes" and structured_payload is not None
        else {}
    )
    structured_match = _structured_category(structured) if structured else None
    if structured_match is not None:
        structured_category, structured_source = structured_match
        # Caller-supplied status reasons are more specific than a generic
        # wrapper message. Preserve an already-specific deterministic match.
        if category == "general_failure" or structured_category == "pod_image":
            category = structured_category
            category_source = structured_source

    ctx: dict = {
        "tool": tool,
        "category": category,
        "category_source": category_source,
        "error_hash": _hash(error_text),
    }
    if structured:
        ctx["request_evidence"] = structured

    # Kubernetes context extraction
    pod_match = re.search(r"pod[/ ]+([a-z0-9][a-z0-9\-\.]+)", error_text, re.I)
    if pod_match:
        ctx["pod"] = pod_match.group(1)

    ns_match = re.search(r"namespace[/ ]+([a-z0-9][a-z0-9\-]+)", error_text, re.I)
    if ns_match:
        ctx["namespace"] = ns_match.group(1)

    deploy_match = re.search(r"deployment[/ ]+([a-z0-9][a-z0-9\-]+)", error_text, re.I)
    if deploy_match:
        ctx["deployment"] = deploy_match.group(1)

    node_match = re.search(r"node[/ ]+([a-z0-9][a-z0-9\-\.]+)", error_text, re.I)
    if node_match:
        ctx["node"] = node_match.group(1)

    # Ansible context extraction
    task_match = re.search(r"TASK\s+\[(.+?)\]", error_text)
    if task_match:
        ctx["task"] = task_match.group(1)

    host_match = re.search(r"fatal:\s+\[(.+?)\]", error_text)
    if host_match:
        ctx["host"] = host_match.group(1)

    helm_match = re.search(r"chart_ref:\s*(\S+)", error_text)
    if helm_match:
        ctx["helm_chart"] = helm_match.group(1)

    return ctx


def _hash(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    normalized = re.sub(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", "<IP>", normalized)
    normalized = re.sub(r"[a-f0-9-]{36}", "<UUID>", normalized)
    normalized = re.sub(r"/[\w/.\-]+", "<PATH>", normalized)
    return hashlib.sha256(normalized.encode()).hexdigest()
