"""Regression tests for node resource investigation."""

from pathlib import Path
import sys

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from k8s.kubectl_runner import runner_ctx, set_runner  # noqa: E402
from k8s.wrappers import get_nodes, get_pods, investigate_node  # noqa: E402


class FakeRunner:
    def __init__(self, nodes, pods):
        self.nodes = nodes
        self.pods = pods

    def run_json(self, args, namespace=None):
        if args == ["get", "nodes", "-o", "json"]:
            return {"items": self.nodes}
        if args == ["get", "namespaces", "-o", "json"]:
            namespaces = sorted({
                pod.get("metadata", {}).get("namespace", "default")
                for pod in self.pods
            })
            return {
                "items": [
                    {"metadata": {"name": ns, "labels": {}}, "status": {"phase": "Active"}}
                    for ns in namespaces
                ]
            }
        if args == ["get", "pods", "-o", "json"]:
            return {
                "items": [
                    pod for pod in self.pods
                    if namespace is None or pod.get("metadata", {}).get("namespace") == namespace
                ]
            }
        if args == ["get", "pods", "--all-namespaces", "-o", "json"]:
            return {"items": self.pods}
        raise AssertionError(f"unexpected kubectl args: {args}")


class FakePodTextRunner:
    def run(self, args, namespace=None, **kwargs):
        assert args == ["get", "pods", "--all-namespaces"]
        assert namespace is None

        class Result:
            stdout = (
                "NAMESPACE     NAME        READY   STATUS    RESTARTS   AGE\n"
                "kube-system   coredns-1    1/1     Running   0          1d\n"
                "default       app-1        1/1     Running   0          2h\n"
                "argocd        api-1        1/1     Running   1          3h\n"
            )
            stderr = ""
            success = True

            def raise_for_status(self):
                return None

        return Result()


def _node(name, labels=None):
    return {
        "metadata": {
            "name": name,
            "labels": labels or {"node-role.kubernetes.io/worker": ""},
            "annotations": {"node.alpha.kubernetes.io/ttl": "0"},
        },
        "spec": {
            "unschedulable": False,
            "taints": [{"key": "dedicated", "value": "apps", "effect": "NoSchedule"}],
        },
        "status": {
            "capacity": {"cpu": "4", "memory": "16Gi", "pods": "110"},
            "allocatable": {"cpu": "3900m", "memory": "15Gi", "pods": "100"},
            "addresses": [
                {"type": "InternalIP", "address": "10.0.0.1"},
                {"type": "Hostname", "address": name},
            ],
            "conditions": [{
                "type": "Ready",
                "status": "True",
                "reason": "KubeletReady",
                "message": "kubelet is posting ready status",
                "lastHeartbeatTime": "2026-01-01T00:00:00Z",
                "lastTransitionTime": "2026-01-01T00:00:00Z",
            }],
            "nodeInfo": {"kubeletVersion": "v1.29.0", "osImage": "linux"},
        },
    }


def _pod(node_name, namespace="apps", name="web-0", labels=None):
    return {
        "metadata": {
            "namespace": namespace,
            "name": name,
            "labels": labels or {"app": "web", "tier": "frontend"},
            "ownerReferences": [{"kind": "ReplicaSet", "name": "web-abc", "controller": True}],
        },
        "spec": {
            "nodeName": node_name,
            "serviceAccountName": "web-sa",
            "nodeSelector": {"disk": "ssd"},
            "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "apps", "effect": "NoSchedule"}],
            "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {}}},
            "volumes": [
                {"name": "settings", "configMap": {"name": "web-config"}},
                {"name": "token", "secret": {"secretName": "web-secret"}},
            ],
            "containers": [
                {
                    "name": "app",
                    "image": "registry.example.com/web:v1",
                    "resources": {
                        "requests": {"cpu": "500m", "memory": "256Mi"},
                        "limits": {"cpu": "1", "memory": "1Gi"},
                    },
                    "env": [
                        {"name": "PUBLIC_MODE", "value": "prod"},
                        {"name": "PASSWORD", "valueFrom": {"secretKeyRef": {"name": "web-secret", "key": "password"}}},
                    ],
                    "envFrom": [{"configMapRef": {"name": "web-config"}}],
                }
            ],
            "initContainers": [
                {
                    "name": "init",
                    "image": "registry.example.com/init:v1",
                    "resources": {
                        "requests": {"cpu": "1", "memory": "512Mi"},
                    },
                }
            ],
        },
        "status": {"phase": "Running"},
    }


def test_investigate_node_resolves_single_partial_fqdn_and_rolls_up_resources():
    node = _node("k8s-worker-01.example.com")
    token = set_runner(FakeRunner([node], [_pod("k8s-worker-01.example.com")]))
    try:
        result = investigate_node("k8s-worker-01")
    finally:
        runner_ctx.reset(token)

    assert result["name"] == "k8s-worker-01.example.com"
    assert result["allocated"]["cpu_requests_millicores"] == 1000
    assert result["allocated"]["cpu_requests_percent_of_allocatable"] == 25.64
    assert result["allocated"]["memory_requests_bytes"] == 512 * 1024 * 1024
    assert result["allocated"]["memory_requests_percent_of_allocatable"] == 3.33
    assert result["allocated"]["memory_limits_bytes"] == 1024 * 1024 * 1024
    assert result["allocated"]["non_terminated_pods"] == 1
    assert result["annotations"] == {"count": 1, "keys": ["node.alpha.kubernetes.io/ttl"]}
    assert result["taints"][0]["key"] == "dedicated"
    assert result["addresses"][0] == {"type": "InternalIP", "address": "10.0.0.1"}
    assert result["conditions"][0]["last_heartbeat_time"] == "2026-01-01T00:00:00Z"


def test_get_nodes_includes_labels_for_all_nodes():
    nodes = [
        _node("node-a", {"node-role.kubernetes.io/worker": "", "zone": "east"}),
        _node("node-b", {"node-role.kubernetes.io/control-plane": "", "env": "prod"}),
    ]
    token = set_runner(FakeRunner(nodes, []))
    try:
        result = get_nodes()
    finally:
        runner_ctx.reset(token)

    assert result["node_count"] == 2
    assert result["labels_only"] is False
    assert result["nodes"][0]["name"] == "node-a"
    assert result["nodes"][0]["labels"] == {
        "node-role.kubernetes.io/worker": "",
        "zone": "east",
    }
    assert result["nodes"][0]["label_count"] == 2
    assert result["nodes"][1]["roles"] == ["control-plane"]


def test_get_nodes_labels_only_filters_inventory_fields():
    nodes = [
        _node("node-a", {"node-role.kubernetes.io/worker": "", "zone": "east"}),
    ]
    token = set_runner(FakeRunner(nodes, []))
    try:
        result = get_nodes(labels_only=True)
    finally:
        runner_ctx.reset(token)

    assert result == {
        "node_count": 1,
        "labels_only": True,
        "nodes": [
            {
                "name": "node-a",
                "labels": {"node-role.kubernetes.io/worker": "", "zone": "east"},
                "label_count": 2,
            }
        ],
    }


def test_get_nodes_includes_scheduling_fields_by_default():
    token = set_runner(FakeRunner([_node("node-a")], []))
    try:
        result = get_nodes()
    finally:
        runner_ctx.reset(token)

    node = result["nodes"][0]
    assert node["annotations"] == {"count": 1, "keys": ["node.alpha.kubernetes.io/ttl"]}
    assert node["taints"] == [{"key": "dedicated", "value": "apps", "effect": "NoSchedule", "time_added": ""}]
    assert node["unschedulable"] is False
    assert node["addresses"] == [
        {"type": "InternalIP", "address": "10.0.0.1"},
        {"type": "Hostname", "address": "node-a"},
    ]
    assert node["conditions"][0]["reason"] == "KubeletReady"


def test_get_nodes_focused_scheduling_modes():
    token = set_runner(FakeRunner([_node("node-a")], []))
    try:
        taints = get_nodes(taints_only=True)
        conditions = get_nodes(conditions_only=True)
        addresses = get_nodes(addresses_only=True)
    finally:
        runner_ctx.reset(token)

    assert taints["focused_modes"] == ["taints"]
    assert taints["nodes"] == [{
        "name": "node-a",
        "taints": [{"key": "dedicated", "value": "apps", "effect": "NoSchedule", "time_added": ""}],
        "unschedulable": False,
    }]
    assert conditions["focused_modes"] == ["conditions"]
    assert conditions["nodes"][0]["conditions"][0]["type"] == "Ready"
    assert addresses["focused_modes"] == ["addresses"]
    assert addresses["nodes"][0]["addresses"][0]["address"] == "10.0.0.1"


def test_investigate_node_returns_structured_ambiguous_partial_match():
    nodes = [
        _node("k8s-worker-01.example.com"),
        _node("k8s-worker-02.example.com"),
    ]
    token = set_runner(FakeRunner(nodes, []))
    try:
        result = investigate_node("k8s-worker")
    finally:
        runner_ctx.reset(token)

    assert result["error"] == "ambiguous_node_name"
    assert result["matches"] == [
        "k8s-worker-01.example.com",
        "k8s-worker-02.example.com",
    ]


def test_get_pods_all_namespaces_can_exclude_kube_prefixes():
    token = set_runner(FakePodTextRunner())
    try:
        result = get_pods("*", exclude_namespace_prefixes=["kube-"])
    finally:
        runner_ctx.reset(token)

    assert result["pod_count"] == 2
    assert result["namespace_summary"] == {"argocd": 1, "default": 1}
    assert result["exclude_namespace_prefixes"] == ["kube-"]
    assert {pod["namespace"] for pod in result["pods"]} == {"argocd", "default"}


def test_get_pods_labels_only_filters_to_pod_labels():
    token = set_runner(FakeRunner([], [_pod("node-a")]))
    try:
        result = get_pods("apps", labels_only=True)
    finally:
        runner_ctx.reset(token)

    assert result["focused_modes"] == ["labels"]
    assert result["pods"] == [{
        "namespace": "apps",
        "name": "web-0",
        "labels": {"app": "web", "tier": "frontend"},
        "label_count": 2,
    }]


def test_get_pods_images_only_filters_to_images():
    token = set_runner(FakeRunner([], [_pod("node-a")]))
    try:
        result = get_pods("apps", images_only=True)
    finally:
        runner_ctx.reset(token)

    assert result["focused_modes"] == ["images"]
    assert result["pods"][0]["images"] == ["registry.example.com/web:v1"]
    assert result["pods"][0]["containers"] == [
        {"name": "app", "image": "registry.example.com/web:v1"}
    ]
    assert result["pods"][0]["init_containers"] == [
        {"name": "init", "image": "registry.example.com/init:v1"}
    ]


def test_get_pods_resources_only_filters_to_requests_and_limits():
    token = set_runner(FakeRunner([], [_pod("node-a")]))
    try:
        result = get_pods("apps", resources_only=True)
    finally:
        runner_ctx.reset(token)

    assert result["focused_modes"] == ["resources"]
    assert result["pods"][0]["containers"] == [{
        "name": "app",
        "resources": {
            "requests": {"cpu": "500m", "memory": "256Mi"},
            "limits": {"cpu": "1", "memory": "1Gi"},
        },
    }]
    assert result["pods"][0]["init_containers"] == [{
        "name": "init",
        "resources": {"requests": {"cpu": "1", "memory": "512Mi"}},
    }]


def test_get_pods_placement_only_filters_to_scheduling_fields():
    token = set_runner(FakeRunner([], [_pod("node-a")]))
    try:
        result = get_pods("apps", placement_only=True)
    finally:
        runner_ctx.reset(token)

    pod = result["pods"][0]
    assert result["focused_modes"] == ["placement"]
    assert pod["node_name"] == "node-a"
    assert pod["service_account_name"] == "web-sa"
    assert pod["node_selector"] == {"disk": "ssd"}
    assert pod["tolerations"][0]["key"] == "dedicated"
    assert pod["affinity"] == {
        "has_node_affinity": True,
        "has_pod_affinity": False,
        "has_pod_anti_affinity": False,
    }
    assert pod["owner_references"] == [{"kind": "ReplicaSet", "name": "web-abc", "controller": True}]


def test_get_pods_details_exposes_safe_env_refs_without_values():
    token = set_runner(FakeRunner([], [_pod("node-a")]))
    try:
        result = get_pods("apps", details=True)
    finally:
        runner_ctx.reset(token)

    container = result["pods"][0]["containers"][0]
    literal_env = next(env for env in container["env"] if env["name"] == "PUBLIC_MODE")
    secret_env = next(env for env in container["env"] if env["name"] == "PASSWORD")
    assert literal_env == {
        "name": "PUBLIC_MODE",
        "value_from": None,
        "has_literal_value": True,
    }
    assert secret_env["value_from"] == {
        "type": "secretKeyRef",
        "name": "web-secret",
        "key": "password",
        "field_path": "",
        "resource": "",
        "optional": None,
    }
    assert "prod" not in str(result["pods"][0]["containers"])
    assert "password" in str(result["pods"][0]["containers"])
