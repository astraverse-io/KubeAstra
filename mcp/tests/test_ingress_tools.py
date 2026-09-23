"""Tests for get_ingress and investigate_ingress tools and endpoints."""

from unittest.mock import MagicMock, patch
import pytest

from k8s.wrappers import parse_ingress, get_ingress, investigate_ingress


@pytest.fixture
def mock_ingress_data():
    return {
        "metadata": {
            "name": "api-ingress",
            "namespace": "prod",
            "creationTimestamp": "2026-09-01T12:00:00Z",
            "labels": {"app": "api-gateway"},
            "annotations": {
                "kubernetes.io/ingress.class": "nginx",
                "cert-manager.io/cluster-issuer": "letsencrypt-prod",
                "auth-secret-key": "supersecretvalue",
            },
        },
        "spec": {
            "ingressClassName": "nginx",
            "tls": [
                {
                    "hosts": ["api.example.com"],
                    "secretName": "api-tls-cert",
                }
            ],
            "rules": [
                {
                    "host": "api.example.com",
                    "http": {
                        "paths": [
                            {
                                "path": "/v1",
                                "pathType": "Prefix",
                                "backend": {
                                    "service": {
                                        "name": "api-service",
                                        "port": {"number": 8080},
                                    }
                                },
                            }
                        ]
                    },
                }
            ],
            "defaultBackend": {
                "service": {
                    "name": "default-http-backend",
                    "port": {"number": 80},
                }
            },
        },
        "status": {
            "loadBalancer": {
                "ingress": [{"ip": "203.0.113.50"}]
            }
        },
    }


def test_parse_ingress_v1_structure(mock_ingress_data):
    parsed = parse_ingress(mock_ingress_data)
    assert parsed["name"] == "api-ingress"
    assert parsed["namespace"] == "prod"
    assert parsed["ingress_class"] == "nginx"
    assert parsed["annotations"]["auth-secret-key"] == "<redacted>"
    assert len(parsed["rules"]) == 1
    assert parsed["rules"][0]["host"] == "api.example.com"
    assert parsed["rules"][0]["paths"][0]["service_name"] == "api-service"
    assert parsed["rules"][0]["paths"][0]["service_port"] == 8080
    assert parsed["default_backend"]["service_name"] == "default-http-backend"
    assert parsed["tls"][0]["secret_name"] == "api-tls-cert"
    assert parsed["load_balancer"][0]["ip"] == "203.0.113.50"


def test_parse_ingress_legacy_structure():
    legacy_data = {
        "metadata": {"name": "legacy-ingress", "namespace": "default"},
        "spec": {
            "rules": [
                {
                    "host": "legacy.example.com",
                    "http": {
                        "paths": [
                            {
                                "path": "/legacy",
                                "backend": {
                                    "serviceName": "legacy-svc",
                                    "servicePort": 80,
                                },
                            }
                        ]
                    },
                }
            ]
        },
    }
    parsed = parse_ingress(legacy_data)
    assert parsed["name"] == "legacy-ingress"
    assert parsed["rules"][0]["paths"][0]["service_name"] == "legacy-svc"
    assert parsed["rules"][0]["paths"][0]["service_port"] == 80


def test_get_ingress_full_and_focused(mock_ingress_data):
    mock_runner = MagicMock()
    mock_runner.run_json.return_value = mock_ingress_data

    with patch("k8s.wrappers.get_runner", return_value=mock_runner):
        # 1. Full output
        res = get_ingress("prod", "api-ingress")
        assert res["name"] == "api-ingress"
        assert res["namespace"] == "prod"
        assert len(res["rules"]) == 1
        assert len(res["backends"]) == 2  # 1 rule path + 1 default backend

        # 2. rules_only
        rules_res = get_ingress("prod", "api-ingress", rules_only=True)
        assert "rules" in rules_res
        assert "tls" not in rules_res
        assert rules_res["focused_modes"] == ["rules"]

        # 3. tls_only
        tls_res = get_ingress("prod", "api-ingress", tls_only=True)
        assert "tls" in tls_res
        assert "rules" not in tls_res
        assert tls_res["focused_modes"] == ["tls"]

        # 4. backends_only
        backends_res = get_ingress("prod", "api-ingress", backends_only=True)
        assert "backends" in backends_res
        assert "rules" not in backends_res
        assert backends_res["focused_modes"] == ["backends"]


def test_investigate_ingress_healthy(mock_ingress_data):
    mock_runner = MagicMock()
    mock_runner.run_json.side_effect = [
        mock_ingress_data,  # get ingress
        {"items": [{"metadata": {"name": "api-tls-cert"}}]},  # get secret api-tls-cert
    ]

    mock_svc = {"name": "api-service", "type": "ClusterIP"}
    mock_default_svc = {"name": "default-http-backend", "type": "ClusterIP"}
    mock_ep = {"ready_count": 3, "not_ready_count": 0}

    with patch("k8s.wrappers.get_runner", return_value=mock_runner), \
         patch("k8s.wrappers.get_service", side_effect=[mock_svc, mock_default_svc]), \
         patch("k8s.wrappers.get_endpoints", return_value=mock_ep):

        report = investigate_ingress("prod", "api-ingress")
        assert report["name"] == "api-ingress"
        assert report["status"] == "HEALTHY"
        assert len(report["findings"]) == 0
        assert "All ingress backend services are active" in report["recommendations"][0]


def test_investigate_ingress_missing_backend(mock_ingress_data):
    mock_runner = MagicMock()
    mock_runner.run_json.side_effect = [
        mock_ingress_data,  # get ingress
        {"items": []},  # get secret
    ]

    with patch("k8s.wrappers.get_runner", return_value=mock_runner), \
         patch("k8s.wrappers.get_service", side_effect=RuntimeError("Service not found")):

        report = investigate_ingress("prod", "api-ingress")
        assert report["status"] == "CRITICAL"
        assert any(f["category"] == "missing_backend_service" for f in report["findings"])
        assert any("Create missing backend Service" in r for r in report["recommendations"])


def test_investigate_ingress_unready_endpoints(mock_ingress_data):
    mock_runner = MagicMock()
    mock_runner.run_json.side_effect = [
        mock_ingress_data,  # get ingress
        {"items": []},  # get secret
    ]

    mock_svc = {"name": "api-service", "type": "ClusterIP"}
    mock_ep_unready = {"ready_count": 0, "not_ready_count": 2}

    with patch("k8s.wrappers.get_runner", return_value=mock_runner), \
         patch("k8s.wrappers.get_service", return_value=mock_svc), \
         patch("k8s.wrappers.get_endpoints", return_value=mock_ep_unready):

        report = investigate_ingress("prod", "api-ingress")
        assert report["status"] == "CRITICAL"
        assert any(f["category"] == "no_ready_endpoints" for f in report["findings"])


def test_investigate_ingress_missing_tls_secret(mock_ingress_data):
    mock_runner = MagicMock()

    def mock_run_json(cmd, namespace=None):
        if "ingress" in cmd:
            return mock_ingress_data
        if "secret" in cmd:
            raise RuntimeError("Secret not found")
        return {}

    mock_runner.run_json.side_effect = mock_run_json
    mock_svc = {"name": "api-service", "type": "ClusterIP"}
    mock_ep = {"ready_count": 2, "not_ready_count": 0}

    with patch("k8s.wrappers.get_runner", return_value=mock_runner), \
         patch("k8s.wrappers.get_service", return_value=mock_svc), \
         patch("k8s.wrappers.get_endpoints", return_value=mock_ep):

        report = investigate_ingress("prod", "api-ingress")
        assert report["status"] == "DEGRADED"
        assert any(f["category"] == "missing_tls_secret" for f in report["findings"])


def test_investigate_ingress_not_found():
    mock_runner = MagicMock()
    mock_runner.run_json.side_effect = RuntimeError("Ingress not found")

    with patch("k8s.wrappers.get_runner", return_value=mock_runner):
        report = investigate_ingress("prod", "nonexistent-ingress")
        assert report["status"] == "CRITICAL"
        assert report["findings"][0]["category"] == "ingress_not_found"
