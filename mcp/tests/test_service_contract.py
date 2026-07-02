"""Regression tests for service routing output contracts."""

from pathlib import Path
import sys

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from k8s.kubectl_runner import runner_ctx, set_runner  # noqa: E402
from k8s.wrappers import get_service, list_services  # noqa: E402


class FakeServiceRunner:
    def __init__(self, services):
        self.services = services

    def run_json(self, args, namespace=None):
        assert namespace == "apps"
        if args == ["get", "service", "web", "-o", "json"]:
            return self.services[0]
        if args == ["get", "services", "-o", "json"]:
            return {"items": self.services}
        raise AssertionError(f"unexpected kubectl args: {args}")


def _load_balancer_service():
    return {
        "metadata": {
            "name": "web",
            "namespace": "apps",
            "labels": {"app": "web"},
            "annotations": {
                "service.beta.kubernetes.io/aws-load-balancer-type": "nlb",
                "external-dns.alpha.kubernetes.io/hostname": "web.example.com",
            },
            "creationTimestamp": "2026-01-01T00:00:00Z",
        },
        "spec": {
            "type": "LoadBalancer",
            "clusterIP": "10.0.0.1",
            "clusterIPs": ["10.0.0.1"],
            "externalIPs": ["192.0.2.10"],
            "selector": {"app": "web"},
            "sessionAffinity": "ClientIP",
            "externalTrafficPolicy": "Local",
            "internalTrafficPolicy": "Cluster",
            "ipFamilies": ["IPv4"],
            "ipFamilyPolicy": "SingleStack",
            "ports": [
                {
                    "name": "http",
                    "protocol": "TCP",
                    "port": 80,
                    "targetPort": 8080,
                    "nodePort": 30080,
                    "appProtocol": "http",
                }
            ],
        },
        "status": {
            "loadBalancer": {
                "ingress": [{"ip": "203.0.113.10", "hostname": "lb.example.com"}]
            }
        },
    }


def _headless_service():
    return {
        "metadata": {"name": "db", "namespace": "apps", "labels": {"app": "db"}},
        "spec": {
            "type": "ClusterIP",
            "clusterIP": "None",
            "selector": {},
            "ports": [{"name": "db", "port": 5432, "targetPort": 5432, "protocol": "TCP"}],
        },
        "status": {},
    }


def _run_get_service(**kwargs):
    token = set_runner(FakeServiceRunner([_load_balancer_service(), _headless_service()]))
    try:
        return get_service("apps", "web", **kwargs)
    finally:
        runner_ctx.reset(token)


def test_get_service_exposes_safe_routing_contract():
    result = _run_get_service()

    assert result["name"] == "web"
    assert result["labels"] == {"app": "web"}
    assert result["annotations"] == {
        "count": 2,
        "keys": [
            "external-dns.alpha.kubernetes.io/hostname",
            "service.beta.kubernetes.io/aws-load-balancer-type",
        ],
    }
    assert result["type"] == "LoadBalancer"
    assert result["cluster_ips"] == ["10.0.0.1"]
    assert result["external_ips"] == ["192.0.2.10"]
    assert result["session_affinity"] == "ClientIP"
    assert result["external_traffic_policy"] == "Local"
    assert result["internal_traffic_policy"] == "Cluster"
    assert result["ip_families"] == ["IPv4"]
    assert result["ip_family_policy"] == "SingleStack"
    assert result["load_balancer"] == {
        "ingress": [{"ip": "203.0.113.10", "hostname": "lb.example.com"}]
    }
    assert result["ports"] == [{
        "name": "http",
        "protocol": "TCP",
        "port": 80,
        "target_port": 8080,
        "app_protocol": "http",
        "node_port": 30080,
    }]
    assert "web.example.com" not in str(result["annotations"])


def test_get_service_focused_modes():
    assert _run_get_service(ports_only=True) == {
        "name": "web",
        "namespace": "apps",
        "type": "LoadBalancer",
        "focused_modes": ["ports"],
        "ports": [{
            "name": "http",
            "protocol": "TCP",
            "port": 80,
            "target_port": 8080,
            "app_protocol": "http",
            "node_port": 30080,
        }],
    }

    selector = _run_get_service(selector_only=True)
    assert selector["focused_modes"] == ["selector"]
    assert selector["selector"] == {"app": "web"}
    assert selector["labels"] == {"app": "web"}

    traffic = _run_get_service(traffic_policy_only=True)
    assert traffic["focused_modes"] == ["traffic_policy"]
    assert traffic["external_traffic_policy"] == "Local"
    assert traffic["session_affinity"] == "ClientIP"
    assert traffic["load_balancer"]["ingress"][0]["ip"] == "203.0.113.10"


def test_list_services_uses_same_safe_service_contract():
    token = set_runner(FakeServiceRunner([_load_balancer_service(), _headless_service()]))
    try:
        result = list_services("apps")
    finally:
        runner_ctx.reset(token)

    assert result["service_count"] == 2
    assert result["services"][0]["ports"][0]["node_port"] == 30080
    assert result["services"][1]["name"] == "db"
    assert result["services"][1]["diagnostic_hint"] == "Service has no selector - may be headless or external"
