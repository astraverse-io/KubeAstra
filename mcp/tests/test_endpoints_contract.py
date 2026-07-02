"""Regression tests for service endpoint and EndpointSlice contracts."""

from pathlib import Path
import sys

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from k8s.kubectl_runner import runner_ctx, set_runner  # noqa: E402
from k8s.wrappers import get_endpoints  # noqa: E402


class FakeEndpointRunner:
    def __init__(self, endpoints, endpoint_slices=None, fail_slices=False):
        self.endpoints = endpoints
        self.endpoint_slices = endpoint_slices or {"items": []}
        self.fail_slices = fail_slices

    def run_json(self, args, namespace=None):
        assert namespace == "apps"
        if args == ["get", "endpoints", "web", "-o", "json"]:
            return self.endpoints
        if args == [
            "get",
            "endpointslices",
            "-l",
            "kubernetes.io/service-name=web",
            "-o",
            "json",
        ]:
            if self.fail_slices:
                raise RuntimeError("endpointslices forbidden")
            return self.endpoint_slices
        raise AssertionError(f"unexpected kubectl args: {args}")


def _legacy_endpoints():
    return {
        "metadata": {"name": "web", "namespace": "apps"},
        "subsets": [
            {
                "addresses": [
                    {
                        "ip": "10.1.1.10",
                        "nodeName": "node-a",
                        "targetRef": {"kind": "Pod", "name": "web-0", "namespace": "apps"},
                    }
                ],
                "notReadyAddresses": [
                    {
                        "ip": "10.1.1.11",
                        "nodeName": "node-b",
                        "targetRef": {"kind": "Pod", "name": "web-1", "namespace": "apps"},
                    }
                ],
                "ports": [{"name": "http", "port": 8080, "protocol": "TCP"}],
            }
        ],
    }


def _endpoint_slices():
    return {
        "items": [
            {
                "metadata": {
                    "name": "web-abc",
                    "namespace": "apps",
                    "labels": {"kubernetes.io/service-name": "web"},
                },
                "addressType": "IPv4",
                "ports": [
                    {
                        "name": "http",
                        "port": 8080,
                        "protocol": "TCP",
                        "appProtocol": "http",
                    }
                ],
                "endpoints": [
                    {
                        "addresses": ["10.1.1.10"],
                        "nodeName": "node-a",
                        "zone": "us-east-1a",
                        "conditions": {"ready": True, "serving": True, "terminating": False},
                        "hints": {"forZones": [{"name": "us-east-1a"}]},
                        "targetRef": {"kind": "Pod", "name": "web-0", "namespace": "apps"},
                    },
                    {
                        "addresses": ["10.1.1.11"],
                        "nodeName": "node-b",
                        "zone": "us-east-1b",
                        "conditions": {"ready": False, "serving": True, "terminating": True},
                        "targetRef": {"kind": "Pod", "name": "web-1", "namespace": "apps"},
                    },
                ],
            }
        ]
    }


def test_get_endpoints_includes_endpoint_slice_readiness_and_topology():
    token = set_runner(FakeEndpointRunner(_legacy_endpoints(), _endpoint_slices()))
    try:
        result = get_endpoints("apps", "web")
    finally:
        runner_ctx.reset(token)

    assert result["name"] == "web"
    assert result["ready_count"] == 1
    assert result["not_ready_count"] == 1
    assert result["endpoint_slice_count"] == 1
    assert result["endpoint_slice_endpoint_count"] == 2

    slices = result["endpoint_slices"]
    assert slices["ready_count"] == 1
    assert slices["not_ready_count"] == 1
    assert slices["serving_count"] == 2
    assert slices["terminating_count"] == 1
    assert slices["ports"] == [{
        "name": "http",
        "port": 8080,
        "protocol": "TCP",
        "app_protocol": "http",
    }]
    assert slices["endpoints"][0]["target_ref"] == {
        "kind": "Pod",
        "name": "web-0",
        "namespace": "apps",
    }
    assert slices["endpoints"][0]["node_name"] == "node-a"
    assert slices["endpoints"][0]["zone"] == "us-east-1a"
    assert slices["endpoints"][0]["hints_for_zones"] == ["us-east-1a"]
    assert slices["endpoints"][1]["conditions"] == {
        "ready": False,
        "serving": True,
        "terminating": True,
    }
    assert result["diagnostic_hint"] == "1 endpoint(s) are terminating"


def test_get_endpoints_gracefully_falls_back_when_slices_unavailable():
    token = set_runner(FakeEndpointRunner(_legacy_endpoints(), fail_slices=True))
    try:
        result = get_endpoints("apps", "web")
    finally:
        runner_ctx.reset(token)

    assert result["has_endpoints"] is True
    assert result["ready_addresses"][0]["target_ref"]["name"] == "web-0"
    assert result["endpoint_slices"]["error"] == "endpointslices forbidden"
    assert result["endpoint_slices"]["endpoint_count"] == 0


def test_get_endpoints_can_skip_endpoint_slices():
    token = set_runner(FakeEndpointRunner(_legacy_endpoints(), fail_slices=True))
    try:
        result = get_endpoints("apps", "web", include_slices=False)
    finally:
        runner_ctx.reset(token)

    assert result["include_slices"] is False
    assert "endpoint_slices" not in result
    assert result["ready_count"] == 1
