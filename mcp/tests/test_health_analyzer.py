"""Unit and integration tests for the Deterministic Kubernetes Health Analyzer."""

from pathlib import Path
import sys

# Ensure mcp root is in path
MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import pytest
from k8s.analyzer import (
    AnalyzerResult,
    EvidenceRef,
    Finding,
    Severity,
    run_health_analyzer,
)
from k8s.analyzer.formatters import format_analyzer_summary
from k8s.analyzer.rules import (
    check_pod_rules,
    check_scheduling_and_node_rules,
    check_service_and_rollout_rules,
)
from tool_registry import dispatch, DispatchContext, resolve_tool


class FakeKubectlRunner:
    """Fake runner providing synthetic Kubernetes object JSON responses."""

    def __init__(self, data_map: dict | None = None):
        self.data_map = data_map or {}

    def run_json(self, args: list[str], namespace: str | None = None) -> dict:
        cmd_key = " ".join(args)
        if cmd_key in self.data_map:
            return self.data_map[cmd_key]

        # General fallbacks by object kind requested
        if "get pods" in cmd_key or "get pod" in cmd_key:
            return {"kind": "List", "items": self.data_map.get("pods", [])}
        if "get events" in cmd_key:
            return {"kind": "List", "items": self.data_map.get("events", [])}
        if "get nodes" in cmd_key or "get node" in cmd_key:
            return {"kind": "List", "items": self.data_map.get("nodes", [])}
        if "get services" in cmd_key:
            return {"kind": "List", "items": self.data_map.get("services", [])}
        if "get endpoints" in cmd_key:
            return {"kind": "List", "items": self.data_map.get("endpoints", [])}
        if "get deployments" in cmd_key:
            return {"kind": "List", "items": self.data_map.get("deployments", [])}
        if "get pvc" in cmd_key:
            return {"kind": "List", "items": self.data_map.get("pvcs", [])}

        return {"kind": "List", "items": []}


# ── 1. Pod Failure Rules Tests ───────────────────────────────────────────────

def test_crashloopbackoff_rule():
    pod_json = {
        "metadata": {"name": "app-crash", "namespace": "prod"},
        "status": {
            "containerStatuses": [
                {
                    "name": "web",
                    "restartCount": 6,
                    "state": {
                        "waiting": {
                            "reason": "CrashLoopBackOff",
                            "message": "back-off 5m0s restarting failed container",
                        }
                    },
                }
            ]
        },
    }
    events_json = [
        {
            "type": "Warning",
            "reason": "BackOff",
            "message": "Back-off restarting failed container web",
            "involvedObject": {"kind": "Pod", "name": "app-crash"},
        }
    ]

    findings = check_pod_rules([pod_json], events_json)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "pod_waiting_crashloopbackoff"
    assert f.severity == Severity.CRITICAL.value
    assert f.namespace == "prod"
    assert f.resource_name == "app-crash"
    assert any(e.value == "CrashLoopBackOff" for e in f.evidence)


def test_image_pull_backoff_rule():
    pod_json = {
        "metadata": {"name": "bad-image-pod", "namespace": "dev"},
        "status": {
            "containerStatuses": [
                {
                    "name": "worker",
                    "restartCount": 0,
                    "state": {
                        "waiting": {
                            "reason": "ImagePullBackOff",
                            "message": "Back-off pulling image repository/app:nonexistent",
                        }
                    },
                }
            ]
        },
    }
    findings = check_pod_rules([pod_json], [])
    assert len(findings) == 1
    assert findings[0].rule_id == "pod_waiting_image_pull"
    assert findings[0].severity == Severity.CRITICAL.value


def test_oomkilled_rule():
    pod_json = {
        "metadata": {"name": "memory-leak-pod", "namespace": "default"},
        "status": {
            "containerStatuses": [
                {
                    "name": "analytics",
                    "restartCount": 3,
                    "state": {"running": {}},
                    "lastState": {
                        "terminated": {
                            "reason": "OOMKilled",
                            "exitCode": 137,
                        }
                    },
                }
            ]
        },
    }
    findings = check_pod_rules([pod_json], [])
    assert len(findings) == 1
    assert findings[0].rule_id == "pod_oomkilled"
    assert findings[0].severity == Severity.CRITICAL.value


def test_high_restart_count_rule():
    pod_json = {
        "metadata": {"name": "flaky-pod", "namespace": "default"},
        "status": {
            "containerStatuses": [
                {
                    "name": "api",
                    "restartCount": 10,
                    "state": {"running": {}},
                }
            ]
        },
    }
    findings = check_pod_rules([pod_json], [], restart_threshold=5)
    assert len(findings) == 1
    assert findings[0].rule_id == "pod_high_restart_count"
    assert findings[0].severity == Severity.WARNING.value


# ── 2. Scheduling & Node Rules Tests ─────────────────────────────────────────

def test_pending_failed_scheduling_rule():
    pod_json = {
        "metadata": {"name": "unschedulable-pod", "namespace": "prod"},
        "status": {"phase": "Pending"},
    }
    events_json = [
        {
            "type": "Warning",
            "reason": "FailedScheduling",
            "message": "0/3 nodes are available: 3 Insufficient cpu.",
            "involvedObject": {"kind": "Pod", "name": "unschedulable-pod"},
        }
    ]
    findings = check_scheduling_and_node_rules([pod_json], [], events_json)
    assert len(findings) == 1
    assert findings[0].rule_id == "pod_pending_failed_scheduling"
    assert findings[0].severity == Severity.WARNING.value


def test_node_not_ready_and_pressure_rules():
    nodes_json = [
        {
            "metadata": {"name": "k8s-node-1"},
            "status": {
                "conditions": [
                    {"type": "Ready", "status": "False", "message": "Kubelet stopped responding"},
                    {"type": "MemoryPressure", "status": "True", "message": "High memory utilization"},
                ]
            },
        }
    ]
    findings = check_scheduling_and_node_rules([], nodes_json, [])
    assert len(findings) == 2
    rule_ids = {f.rule_id for f in findings}
    assert "node_not_ready" in rule_ids
    assert "node_pressure" in rule_ids


# ── 3. Service, Endpoint & Rollout Rules Tests ─────────────────────────────

def test_service_no_endpoints_rule():
    services_json = [
        {
            "metadata": {"name": "frontend-svc", "namespace": "default"},
            "spec": {"selector": {"app": "frontend"}, "type": "ClusterIP"},
        }
    ]
    endpoints_json = [
        {
            "metadata": {"name": "frontend-svc", "namespace": "default"},
            "subsets": [],  # empty
        }
    ]
    findings = check_service_and_rollout_rules(services_json, endpoints_json, [], [])
    assert len(findings) == 1
    assert findings[0].rule_id == "service_no_endpoints"


def test_rollout_progress_deadline_exceeded_rule():
    deployments_json = [
        {
            "metadata": {"name": "payment-dep", "namespace": "prod"},
            "status": {
                "conditions": [
                    {
                        "type": "Progressing",
                        "reason": "ProgressDeadlineExceeded",
                        "message": "ReplicaSet payment-dep-xyz failed to progress",
                    }
                ]
            },
        }
    ]
    findings = check_service_and_rollout_rules([], [], deployments_json, [])
    assert len(findings) == 1
    assert findings[0].rule_id == "rollout_progress_deadline_exceeded"
    assert findings[0].severity == Severity.CRITICAL.value


def test_pvc_pending_rule():
    pvcs_json = [
        {
            "metadata": {"name": "data-pvc", "namespace": "storage"},
            "status": {"phase": "Pending"},
        }
    ]
    findings = check_service_and_rollout_rules([], [], [], pvcs_json)
    assert len(findings) == 1
    assert findings[0].rule_id == "pvc_pending"


# ── 4. Engine & Formatter Integration Tests ─────────────────────────────────

def test_run_health_analyzer_engine():
    fake_data = {
        "pods": [
            {
                "metadata": {"name": "crashing-pod", "namespace": "demo"},
                "status": {
                    "containerStatuses": [
                        {
                            "name": "worker",
                            "restartCount": 8,
                            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                        }
                    ]
                },
            }
        ],
        "events": [],
    }
    runner = FakeKubectlRunner(fake_data)
    result = run_health_analyzer(scope_type="namespace", namespace="demo", runner=runner)

    assert isinstance(result, AnalyzerResult)
    assert result.finding_count == 1
    assert result.summary["critical"] == 1
    assert result.findings[0].rule_id == "pod_waiting_crashloopbackoff"

    summary_text = format_analyzer_summary(result)
    assert "Kubernetes Health Analysis" in summary_text
    assert "CrashLoopBackOff" in summary_text


# ── 5. Tool Registry Dispatch Test ───────────────────────────────────────────

def test_tool_registry_analyze_k8s_health_dispatch():
    tool_def = resolve_tool("analyze_k8s_health")
    assert tool_def is not None
    assert tool_def.name == "analyze_k8s_health"
    assert tool_def.category == "investigation"

    ctx = DispatchContext(surface="mcp")
    res = dispatch("analyze_k8s_health", {"scope_type": "namespace", "namespace": "default"}, ctx)
    assert "finding_count" in res
    assert "summary" in res
    assert "findings" in res
