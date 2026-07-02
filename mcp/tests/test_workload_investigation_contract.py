"""Regression tests for workload investigation output contracts."""

from pathlib import Path
import sys

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from k8s.kubectl_runner import runner_ctx, set_runner  # noqa: E402
from k8s.wrappers import investigate_workload  # noqa: E402


class FakeWorkloadRunner:
    def run_json(self, args, namespace=None):
        assert namespace == "apps"
        if args == ["get", "deployment", "web", "-o", "json"]:
            return _deployment()
        if args == ["get", "pods", "-l", "app=web", "-o", "json"]:
            return {"items": [_pod()]}
        if args == ["get", "events", "--field-selector", "involvedObject.name=web", "-o", "json"]:
            return {"items": [_event()]}
        raise AssertionError(f"unexpected kubectl args: {args}")


class FakePlainSelectorRunner:
    def run_json(self, args, namespace=None):
        assert namespace == "apps"
        if args == ["get", "deployment", "plain", "-o", "json"]:
            deployment = _deployment()
            deployment["metadata"]["name"] = "plain"
            deployment["spec"]["selector"] = {"app": "web"}
            return deployment
        if args == ["get", "pods", "-l", "app=web", "-o", "json"]:
            return {"items": [_pod()]}
        if args == ["get", "events", "--field-selector", "involvedObject.name=plain", "-o", "json"]:
            return {"items": []}
        raise AssertionError(f"unexpected kubectl args: {args}")


def _deployment():
    return {
        "kind": "Deployment",
        "metadata": {
            "name": "web",
            "namespace": "apps",
            "labels": {"app": "web"},
            "annotations": {"deployment.kubernetes.io/revision": "3"},
            "generation": 5,
            "managedFields": [{"large": "field"}],
        },
        "spec": {
            "replicas": 3,
            "selector": {"matchLabels": {"app": "web"}},
            "template": {
                "metadata": {"labels": {"app": "web"}},
                "spec": {
                    "serviceAccountName": "web-sa",
                    "containers": [
                        {
                            "name": "app",
                            "image": "registry.example.com/web:v1",
                            "resources": {"requests": {"cpu": "500m"}},
                            "env": [{"name": "PUBLIC_MODE", "value": "prod"}],
                        }
                    ],
                },
            },
        },
        "status": {
            "replicas": 3,
            "updatedReplicas": 2,
            "readyReplicas": 2,
            "availableReplicas": 2,
            "unavailableReplicas": 1,
            "observedGeneration": 5,
            "conditions": [{
                "type": "Available",
                "status": "False",
                "reason": "MinimumReplicasUnavailable",
                "message": "not enough replicas",
            }],
        },
    }


def _pod():
    return {
        "metadata": {"name": "web-0", "namespace": "apps", "labels": {"app": "web"}},
        "spec": {
            "nodeName": "node-a",
            "containers": [{"name": "app", "image": "registry.example.com/web:v1"}],
        },
        "status": {
            "phase": "Running",
            "containerStatuses": [{"name": "app", "ready": True, "restartCount": 0}],
        },
    }


def _event():
    return {
        "metadata": {"name": "web-event", "namespace": "apps", "creationTimestamp": "2026-01-01T00:00:00Z"},
        "type": "Warning",
        "reason": "FailedCreate",
        "message": "failed to create pod",
        "involvedObject": {"kind": "Deployment", "name": "web", "namespace": "apps"},
        "count": 2,
        "lastTimestamp": "2026-01-01T00:01:00Z",
    }


def test_investigate_workload_adds_structured_summary_related_pods_and_events():
    token = set_runner(FakeWorkloadRunner())
    try:
        result = investigate_workload("apps", "web", "deployment", use_ai=False)
    finally:
        runner_ctx.reset(token)

    summary = result["workload_summary"]
    assert summary["name"] == "web"
    assert summary["replicas"]["ready"] == 2
    assert summary["health_status"] == "unhealthy"
    assert summary["revision"] == "3"
    assert summary["pod_template"]["service_account_name"] == "web-sa"
    assert summary["pod_template"]["images"] == ["registry.example.com/web:v1"]
    assert "prod" not in str(summary["pod_template"]["containers"])
    assert "managedFields" not in result["describe"]

    related = result["related_pods_summary"]
    assert related["selector"] == "app=web"
    assert related["pod_count"] == 1
    assert related["status_breakdown"] == {"Running": 1}
    assert related["pods"][0]["node_name"] == "node-a"

    assert result["events_parsed"]["event_count"] == 1
    assert result["events_parsed"]["events"][0]["reason"] == "FailedCreate"


def test_investigate_workload_handles_plain_selector_maps():
    token = set_runner(FakePlainSelectorRunner())
    try:
        result = investigate_workload("apps", "plain", "deployment", use_ai=False)
    finally:
        runner_ctx.reset(token)

    assert result["related_pods_summary"]["selector"] == "app=web"
    assert result["related_pods_summary"]["pod_count"] == 1
