"""Prompt relevance regression tests.

These tests protect the user-visible answer shape, not just raw tool data.
They are intentionally deterministic: no live cluster and no real LLM calls.
"""

from pathlib import Path
import sys

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from react import _truncate_observation  # noqa: E402
from routers import chat  # noqa: E402
from routers.chat import (  # noqa: E402
    _friendly_summary,
    _keyword_route,
    _simple_pod_status_inventory_prompt,
    _synthesize_answer,
)


def _node_cpu_result() -> dict:
    return {
        "name": "k8s-worker-01",
        "query": "k8s-worker-01",
        "status": "Ready",
        "roles": ["worker"],
        "labels": {"flannel.alpha.coreos.com/public-ip": "10.0.0.50"},
        "annotations": {"keys": ["flannel.alpha.coreos.com/backend-data"]},
        "capacity": {"cpu": "16", "cpu_millicores": 16000, "memory_gib": 31.085},
        "allocatable": {"cpu": "16", "cpu_millicores": 16000, "memory_gib": 30.987},
        "allocated": {
            "cpu_requests_millicores": 300,
            "cpu_requests_cores": 0.3,
            "cpu_requests_percent_of_allocatable": 1.88,
            "cpu_limits_millicores": 150,
            "cpu_limits_cores": 0.15,
            "cpu_limits_percent_of_allocatable": 0.94,
            "memory_requests_gib": 0.262,
            "memory_requests_percent_of_allocatable": 0.84,
            "memory_limits_gib": 0.188,
            "memory_limits_percent_of_allocatable": 0.61,
            "non_terminated_pods": 6,
        },
        "pods": [
            {"namespace": "kube-system", "name": "kube-proxy", "cpu_requests_millicores": 0},
            {"namespace": "monitoring", "name": "node-exporter", "cpu_requests_millicores": 100},
        ],
    }


def test_prompt_routes_single_node_cpu_allocation_to_investigate_node():
    route = _keyword_route("cpu allocated to k8s-worker-01")

    assert route["tool"] == "investigate_node"
    assert route["params"] == {"node_name": "k8s-worker-01"}


def test_prompt_routes_all_node_labels_to_focused_node_listing():
    route = _keyword_route("get all node labels for all nodes")

    assert route["tool"] == "get_nodes"
    assert route["params"] == {"labels_only": True}


def test_node_cpu_fallback_answer_is_narrow_and_relevant():
    reply = _friendly_summary("investigate_node", _node_cpu_result(), "node details")

    assert "0.3 cores requested" in reply
    assert "0.15 cores limited" in reply
    assert "16 allocatable cores" in reply
    assert "6 non-terminated pods" in reply
    assert "flannel.alpha" not in reply
    assert "backend-data" not in reply


def test_node_cpu_synthesis_receives_compact_relevant_evidence(monkeypatch):
    captured = {}

    class Provider:
        enabled = True

        def generate(self, prompt, system=None, temperature=0.1, max_tokens=800):
            captured["prompt"] = prompt
            captured["system"] = system
            captured["max_tokens"] = max_tokens
            return "Node CPU answer"

    monkeypatch.setattr(chat, "_llm_provider", lambda: Provider())

    answer, error = _synthesize_answer(
        "cpu allocated to k8s-worker-01",
        "investigate_node",
        _node_cpu_result(),
    )

    assert error is None
    assert answer == "Node CPU answer"
    assert '"cpu_requests_cores": 0.3' in captured["prompt"]
    assert '"cpu_limits_cores": 0.15' in captured["prompt"]
    assert "flannel.alpha" not in captured["prompt"]
    assert "For node CPU/resource allocation questions" in captured["system"]
    assert captured["max_tokens"] == 800


def test_react_node_observation_keeps_allocation_not_label_noise():
    text = _truncate_observation(_node_cpu_result(), "investigate_node")

    assert '"cpu_requests_cores": 0.3' in text
    assert '"cpu_limits_cores": 0.15' in text
    assert '"non_terminated_pods": 6' in text
    assert "flannel.alpha" not in text


def test_prompt_routes_pod_images_to_focused_pod_inventory():
    route = _keyword_route("what images are running in all pods")

    assert route["tool"] == "get_pods"
    assert route["params"]["namespace"] == "*"
    assert route["params"]["images_only"] is True


def test_prompt_routes_pod_resources_to_focused_pod_inventory():
    route = _keyword_route("show resource requests and limits for pods in k8s-devops")

    assert route["tool"] == "get_pods"
    assert route["params"]["namespace"] == "k8s-devops"
    assert route["params"]["resources_only"] is True


def test_prompt_routes_simple_crashloop_question_to_filtered_pod_inventory():
    route = _keyword_route("any pods in crashloop state?")
    status_route = _keyword_route("any pods in crashloop status")

    assert route["tool"] == "get_pods"
    assert route["params"] == {"namespace": "*", "status_filter": "CrashLoopBackOff"}
    assert status_route["params"] == {"namespace": "*", "status_filter": "CrashLoopBackOff"}
    assert _simple_pod_status_inventory_prompt("any pods in crashloop status") is True


def test_prompt_keeps_kafka_crashloop_root_cause_in_investigation_path():
    prompt = "can you help me identify why kafka pods are in crashlopp status"

    assert _simple_pod_status_inventory_prompt(prompt) is False


@pytest.mark.parametrize(
    ("prompt", "expected_tool", "expected_params"),
    [
        ("cpu allocated to node-a", "investigate_node", {"node_name": "node-a"}),
        ("get all node labels for all nodes", "get_nodes", {"labels_only": True}),
        ("show taints for all nodes", "get_nodes", {"taints_only": True}),
        ("show node addresses for all nodes", "get_nodes", {"addresses_only": True}),
        ("what images are running in all pods", "get_pods", {"namespace": "*", "images_only": True}),
        ("show resource requests and limits for pods in k8s-devops", "get_pods", {"namespace": "k8s-devops", "resources_only": True}),
        ("show pods in infrastructure namespace", "get_pods", {"namespace": "infrastructure"}),
        ("any pods in CrashLoopBackOff state", "get_pods", {"namespace": "*", "status_filter": "CrashLoopBackOff"}),
        ("show pods in imagepullbackoff", "get_pods", {"namespace": "*", "status_filter": "ImagePullBackOff"}),
        ("any pending pods", "get_pods", {"namespace": "*", "status_filter": "Pending"}),
        ("What clusters do I have configured?", "list_contexts", {}),
    ],
)
def test_golden_prompt_route_matrix(prompt, expected_tool, expected_params):
    route = _keyword_route(prompt)

    assert route["tool"] == expected_tool
    assert route["params"] == expected_params


@pytest.mark.parametrize(
    ("prompt", "is_simple_inventory"),
    [
        ("any pods in crashloop status", True),
        ("show pods in imagepullbackoff", True),
        ("any pending pods", True),
        ("why are kafka pods crashing", False),
        ("can you help me identify why kafka pods are in crashlopp status", False),
        ("debug pod my-kafka-0 crashloop", False),
        ("I have a pod in CrashLoopBackOff... How do I figure out what's wrong?", False),
    ],
)
def test_golden_prompt_status_inventory_vs_root_cause_matrix(prompt, is_simple_inventory):
    assert _simple_pod_status_inventory_prompt(prompt) is is_simple_inventory
