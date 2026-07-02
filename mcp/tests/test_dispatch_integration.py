"""Focused tests for registry dispatch reliability behavior."""

from pathlib import Path
import sys

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from tool_registry import (  # noqa: E402
    DispatchContext,
    build_react_tool_descriptions,
    dispatch,
)


def test_dispatch_normalizes_aliases_and_params(monkeypatch):
    calls = []

    def fake_investigate_node(node_name):
        calls.append(node_name)
        return {"name": node_name}

    monkeypatch.setattr("k8s.wrappers.investigate_node", fake_investigate_node)

    result = dispatch(
        "describe_node",
        {"name": "node-a.example.com"},
        DispatchContext(surface="chat"),
    )

    assert result == {"name": "node-a.example.com"}
    assert calls == ["node-a.example.com"]


def test_dispatch_returns_structured_unknown_tool_error():
    result = dispatch("investigate_nodes", {}, DispatchContext(surface="chat"))

    assert result["error"] == "unknown_tool"
    assert result["tool"] == "investigate_nodes"
    assert "investigate_node" in result["valid_tools"]


def test_dispatch_validates_required_params():
    result = dispatch("investigate_node", {}, DispatchContext(surface="chat"))

    assert result["error"] == "invalid_params"
    assert result["tool"] == "investigate_node"
    assert "expected_schema" in result


def test_dispatch_get_pods_passes_namespace_exclusions(monkeypatch):
    calls = []

    def fake_get_pods(namespace, label_selector=None, status_filter=None,
                      exclude_namespaces=None, exclude_namespace_prefixes=None,
                      labels_only=False, images_only=False, resources_only=False,
                      placement_only=False, details=False):
        calls.append({
            "namespace": namespace,
            "label_selector": label_selector,
            "status_filter": status_filter,
            "exclude_namespaces": exclude_namespaces,
            "exclude_namespace_prefixes": exclude_namespace_prefixes,
            "labels_only": labels_only,
            "images_only": images_only,
            "resources_only": resources_only,
            "placement_only": placement_only,
            "details": details,
        })
        return {"pods": [], "pod_count": 0}

    monkeypatch.setattr("k8s.wrappers.get_pods", fake_get_pods)

    result = dispatch(
        "get_pods",
        {"namespace": "*", "exclude_namespace_prefixes": ["kube-"]},
        DispatchContext(surface="chat"),
    )

    assert result == {"pods": [], "pod_count": 0}
    assert calls == [{
        "namespace": "*",
        "label_selector": None,
        "status_filter": None,
        "exclude_namespaces": None,
        "exclude_namespace_prefixes": ["kube-"],
        "labels_only": False,
        "images_only": False,
        "resources_only": False,
        "placement_only": False,
        "details": False,
    }]


def test_dispatch_get_pods_passes_focused_modes(monkeypatch):
    calls = []

    def fake_get_pods(namespace, label_selector=None, status_filter=None,
                      exclude_namespaces=None, exclude_namespace_prefixes=None,
                      labels_only=False, images_only=False, resources_only=False,
                      placement_only=False, details=False):
        calls.append({
            "namespace": namespace,
            "labels_only": labels_only,
            "images_only": images_only,
            "resources_only": resources_only,
            "placement_only": placement_only,
            "details": details,
        })
        return {"pods": [], "pod_count": 0, "focused_modes": ["images"]}

    monkeypatch.setattr("k8s.wrappers.get_pods", fake_get_pods)

    result = dispatch(
        "get_pods",
        {"namespace": "*", "imagesOnly": True},
        DispatchContext(surface="chat"),
    )

    assert result == {"pods": [], "pod_count": 0, "focused_modes": ["images"]}
    assert calls == [{
        "namespace": "*",
        "labels_only": False,
        "images_only": True,
        "resources_only": False,
        "placement_only": False,
        "details": False,
    }]


def test_dispatch_get_nodes_passes_labels_only(monkeypatch):
    calls = []

    def fake_get_nodes(node_name=None, labels_only=False, taints_only=False,
                       conditions_only=False, addresses_only=False):
        calls.append({
            "node_name": node_name,
            "labels_only": labels_only,
            "taints_only": taints_only,
            "conditions_only": conditions_only,
            "addresses_only": addresses_only,
        })
        return {"node_count": 0, "labels_only": labels_only, "nodes": []}

    monkeypatch.setattr("k8s.wrappers.get_nodes", fake_get_nodes)

    result = dispatch(
        "get_nodes",
        {"labels_only": True},
        DispatchContext(surface="chat"),
    )

    assert result == {"node_count": 0, "labels_only": True, "nodes": []}
    assert calls == [{
        "node_name": None,
        "labels_only": True,
        "taints_only": False,
        "conditions_only": False,
        "addresses_only": False,
    }]


def test_dispatch_get_nodes_passes_scheduling_modes(monkeypatch):
    calls = []

    def fake_get_nodes(node_name=None, labels_only=False, taints_only=False,
                       conditions_only=False, addresses_only=False):
        calls.append({
            "node_name": node_name,
            "labels_only": labels_only,
            "taints_only": taints_only,
            "conditions_only": conditions_only,
            "addresses_only": addresses_only,
        })
        return {"node_count": 0, "focused_modes": ["taints"], "nodes": []}

    monkeypatch.setattr("k8s.wrappers.get_nodes", fake_get_nodes)

    result = dispatch(
        "get_nodes",
        {"taintsOnly": True},
        DispatchContext(surface="chat"),
    )

    assert result == {"node_count": 0, "focused_modes": ["taints"], "nodes": []}
    assert calls == [{
        "node_name": None,
        "labels_only": False,
        "taints_only": True,
        "conditions_only": False,
        "addresses_only": False,
    }]


def test_dispatch_get_deployment_passes_focused_modes(monkeypatch):
    calls = []

    def fake_get_deployment(namespace, deployment_name, labels_only=False,
                            images_only=False, resources_only=False,
                            template_only=False):
        calls.append({
            "namespace": namespace,
            "deployment_name": deployment_name,
            "labels_only": labels_only,
            "images_only": images_only,
            "resources_only": resources_only,
            "template_only": template_only,
        })
        return {"name": deployment_name, "focused_modes": ["resources"]}

    monkeypatch.setattr("k8s.wrappers.get_deployment", fake_get_deployment)

    result = dispatch(
        "get_deployment",
        {"namespace": "apps", "deploymentName": "web", "resourcesOnly": True},
        DispatchContext(surface="chat"),
    )

    assert result == {"name": "web", "focused_modes": ["resources"]}
    assert calls == [{
        "namespace": "apps",
        "deployment_name": "web",
        "labels_only": False,
        "images_only": False,
        "resources_only": True,
        "template_only": False,
    }]


def test_dispatch_get_endpoints_passes_include_slices(monkeypatch):
    calls = []

    def fake_get_endpoints(namespace, service_name, include_slices=True):
        calls.append({
            "namespace": namespace,
            "service_name": service_name,
            "include_slices": include_slices,
        })
        return {"name": service_name, "include_slices": include_slices}

    monkeypatch.setattr("k8s.wrappers.get_endpoints", fake_get_endpoints)

    result = dispatch(
        "get_endpoints",
        {"namespace": "apps", "serviceName": "web", "includeSlices": False},
        DispatchContext(surface="chat"),
    )

    assert result == {"name": "web", "include_slices": False}
    assert calls == [{
        "namespace": "apps",
        "service_name": "web",
        "include_slices": False,
    }]


def test_dispatch_get_service_passes_focused_modes(monkeypatch):
    calls = []

    def fake_get_service(namespace, service_name, ports_only=False,
                         selector_only=False, traffic_policy_only=False):
        calls.append({
            "namespace": namespace,
            "service_name": service_name,
            "ports_only": ports_only,
            "selector_only": selector_only,
            "traffic_policy_only": traffic_policy_only,
        })
        return {"name": service_name, "focused_modes": ["ports"]}

    monkeypatch.setattr("k8s.wrappers.get_service", fake_get_service)

    result = dispatch(
        "get_service",
        {"namespace": "apps", "serviceName": "web", "portsOnly": True},
        DispatchContext(surface="chat"),
    )

    assert result == {"name": "web", "focused_modes": ["ports"]}
    assert calls == [{
        "namespace": "apps",
        "service_name": "web",
        "ports_only": True,
        "selector_only": False,
        "traffic_policy_only": False,
    }]


def test_react_descriptions_are_registry_generated():
    descriptions = build_react_tool_descriptions()

    assert "investigate_node(node_name)" in descriptions
    assert "get_nodes(" in descriptions
    assert "get all node labels for all nodes" in descriptions
    assert "kb_search(" in descriptions
    assert "aliases: describe_node" in descriptions


def test_resolve_tool_casing_and_synonym_resolution():
    from tool_registry import resolve_tool

    # 1. camelCase to snake_case
    t1 = resolve_tool("getPods")
    assert t1 is not None
    assert t1.name == "get_pods"

    # 2. kebab-case to snake_case
    t2 = resolve_tool("list-namespaces")
    assert t2 is not None
    assert t2.name == "get_namespaces"

    # 3. space-delimited to snake_case
    t3 = resolve_tool("list pods")
    assert t3 is not None
    assert t3.name == "get_pods"

    # 4. Synonym resolution
    t4 = resolve_tool("get_pod")
    assert t4 is not None
    assert t4.name == "get_pods"

    t5 = resolve_tool("describe_deployment")
    assert t5 is not None
    assert t5.name == "get_deployment"

    # 5. Invalid tool returns None
    t6 = resolve_tool("nonexistent_tool_xyz")
    assert t6 is None


def test_dispatch_switch_context_session_isolated(monkeypatch):
    import sys
    from unittest.mock import MagicMock
    from tool_registry import dispatch, DispatchContext
    from k8s.kubectl_runner import KubectlResult

    db_mock = MagicMock()
    db_mock.get_cluster_connection.return_value = {
        "mode": "kubeconfig-upload",
        "kubeconfig_path": "/tmp/test-kubeconfig"
    }
    monkeypatch.setitem(sys.modules, "db", db_mock)

    # Mock KubectlRunner to return a successful result
    class FakeRunner:
        def __init__(self, kubeconfig_path=None, context=None):
            self.kubeconfig_path = kubeconfig_path
            self.context = context

        def run(self, args, **kwargs):
            return KubectlResult(stdout="{}", stderr="", returncode=0, command=args, duration_seconds=0.1)

    fake_runner = FakeRunner(kubeconfig_path="/tmp/test-kubeconfig")
    monkeypatch.setattr("k8s.kubectl_runner.get_runner", lambda: fake_runner)
    monkeypatch.setattr("k8s.wrappers.get_runner", lambda: fake_runner)
    monkeypatch.setattr("k8s.kubectl_runner.KubectlRunner", FakeRunner)

    result = dispatch(
        "switch_context",
        {"context_name": "my-new-context"},
        DispatchContext(surface="chat", session_id="session-123")
    )

    assert result["success"] is True
    assert result["context_name"] == "my-new-context"
    db_mock.save_cluster_connection.assert_called_once_with(
        session_id="session-123",
        mode="kubeconfig-upload",
        context_name="my-new-context",
        cluster_name="my-new-context",
        server_url="",
        namespace="default",
        kubeconfig_path="/tmp/test-kubeconfig"
    )


def test_list_kubeconfig_contexts_uses_runner(monkeypatch):
    from k8s.wrappers import list_kubeconfig_contexts, get_current_context
    from k8s.kubectl_runner import KubectlResult

    class FakeRunner:
        def __init__(self):
            self.context = "runner-context"

        def run(self, args, **kwargs):
            if "get-contexts" in args:
                return KubectlResult(stdout="context-1\ncontext-2", stderr="", returncode=0, command=args, duration_seconds=0.1)
            elif "current-context" in args:
                return KubectlResult(stdout="runner-context", stderr="", returncode=0, command=args, duration_seconds=0.1)
            elif "view" in args:
                return KubectlResult(stdout="value", stderr="", returncode=0, command=args, duration_seconds=0.1)
            return KubectlResult(stdout="", stderr="", returncode=0, command=args, duration_seconds=0.1)

    fake_runner = FakeRunner()
    monkeypatch.setattr("k8s.wrappers.get_runner", lambda: fake_runner)

    res_list = list_kubeconfig_contexts()
    assert res_list["success"] is True
    assert res_list["contexts"] == ["context-1", "context-2"]
    assert res_list["current_context"] == "runner-context"

    res_curr = get_current_context()
    assert res_curr["success"] is True
    assert res_curr["current_context"] == "runner-context"
    assert res_curr["namespace"] == "value"


def test_k8sgpt_stays_on_simple_optional_path(monkeypatch):
    from k8s.wrappers import k8sgpt_analyze
    from config.settings import settings

    # Enable k8sgpt in settings for this test
    monkeypatch.setattr(settings, "enable_k8sgpt", True)

    calls = []

    class FakeCompleted:
        stdout = "[]"
        stderr = ""
        returncode = 0

    def fake_subprocess_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeCompleted()

    monkeypatch.setattr("subprocess.run", fake_subprocess_run)

    result = k8sgpt_analyze(namespace="kube-system")

    assert result["success"] is True
    assert calls == [["k8sgpt", "analyze", "--output", "json", "--namespace", "kube-system"]]


def test_dispatch_react_surface_returns_tool_envelope(monkeypatch):
    """Verify that when surface is react, result is wrapped in ToolEnvelope."""
    def fake_get_namespaces():
        return {"namespaces": ["default", "kube-system"]}

    monkeypatch.setattr("k8s.wrappers.get_namespaces", fake_get_namespaces)

    from services.tool_envelope import ToolEnvelope
    result = dispatch(
        "get_namespaces",
        {},
        DispatchContext(surface="react"),
    )
    assert isinstance(result, ToolEnvelope)
    assert result.verdict == "n/a"
    assert result.evidence.total_count == 2




