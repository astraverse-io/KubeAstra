"""Regression tests for deterministic namespace analysis contracts."""

from pathlib import Path
import sys

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import k8s.wrappers as wrappers  # noqa: E402


def test_analyze_namespace_adds_deterministic_issue_summary_without_ai(monkeypatch):
    resources = {
        "summary": {"pods": 2, "services": 2, "deployments": 1},
        "pods": [
            {"namespace": "apps", "name": "web-0", "labels": {"app": "web"}, "status": "Running", "ready": True, "restarts": 0},
            {"namespace": "apps", "name": "web-1", "labels": {"app": "web"}, "status": "CrashLoopBackOff", "ready": False, "restarts": 7},
        ],
        "deployments": [
            {"namespace": "apps", "name": "web", "replicas": 3, "ready": 2, "unavailable": 1},
        ],
        "statefulsets": [],
        "daemonsets": [],
        "services": [
            {"namespace": "apps", "name": "web", "selector": {"app": "web"}},
            {"namespace": "apps", "name": "orphan", "selector": {"app": "missing"}},
            {"namespace": "apps", "name": "external", "selector": {}},
        ],
    }
    events = {
        "events": [
            {
                "type": "Warning",
                "reason": "BackOff",
                "message": "Back-off restarting failed container",
                "count": 3,
                "involved_object": {"kind": "Pod", "name": "web-1"},
            }
        ]
    }

    monkeypatch.setattr(wrappers, "list_namespace_resources", lambda namespace: resources)
    monkeypatch.setattr(wrappers, "get_events", lambda namespace, field_selector=None: events)
    monkeypatch.setattr(
        wrappers,
        "get_endpoints",
        lambda namespace, service_name: (
            {
                "has_endpoints": False,
                "ready_count": 0,
                "not_ready_count": 0,
                "diagnostic_hint": "legacy Endpoints empty, EndpointSlices are ready",
                "endpoint_slices": {"ready_count": 1},
            }
            if service_name == "web"
            else {
                "has_endpoints": False,
                "ready_count": 0,
                "not_ready_count": 1,
                "diagnostic_hint": "EndpointSlice endpoints exist, but none are ready",
                "endpoint_slices": {"ready_count": 0},
            }
        ),
    )
    monkeypatch.setattr(wrappers, "_ai_service_available", False)
    monkeypatch.setattr(wrappers, "_llm_service", None)

    result = wrappers.analyze_namespace("apps")

    summary = result["issue_summary"]
    assert summary["unhealthy_pod_count"] == 1
    assert summary["unhealthy_pods"][0]["name"] == "web-1"
    assert summary["unavailable_workload_count"] == 1
    assert summary["unavailable_workloads"][0] == {
        "kind": "deployment",
        "name": "web",
        "namespace": "apps",
        "desired": 3,
        "ready": 2,
        "unavailable": 1,
    }
    assert summary["warning_event_group_count"] == 1
    assert summary["warning_event_groups"][0]["count"] == 3
    assert summary["service_endpoint_checks"][0]["service"] == "web"
    assert summary["service_endpoint_checks"][0]["has_ready_endpoints"] is True
    assert summary["service_endpoint_checks"][0]["endpoint_slice_ready_count"] == 1
    assert summary["service_endpoint_checks"][1]["service"] == "orphan"
    assert summary["service_endpoint_checks"][1]["selector_matches_pods"] is False
    assert summary["service_endpoint_checks"][2]["reason"] == "service has no selector"
    assert summary["services_without_ready_endpoints_count"] == 1
    assert summary["services_without_ready_endpoints"][0]["service"] == "orphan"
    assert summary["services_with_selector_mismatch_count"] == 1
    assert summary["services_with_selector_mismatch"][0]["service"] == "orphan"
    assert result["ai"] == {"ai_enabled": False, "message": "AI service not available"}
