"""Regression tests for deployment output contracts."""

from pathlib import Path
import sys

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from k8s.kubectl_runner import runner_ctx, set_runner  # noqa: E402
from k8s.wrappers import get_deployment  # noqa: E402


class FakeDeploymentRunner:
    def __init__(self, deployment):
        self.deployment = deployment

    def run_json(self, args, namespace=None):
        assert args == ["get", "deployment", "web", "-o", "json"]
        assert namespace == "apps"
        return self.deployment


def _deployment():
    return {
        "metadata": {
            "name": "web",
            "namespace": "apps",
            "labels": {"app": "web", "tier": "frontend"},
            "annotations": {
                "deployment.kubernetes.io/revision": "7",
                "checksum/config": "abc123",
            },
            "generation": 9,
            "creationTimestamp": "2026-01-01T00:00:00Z",
        },
        "spec": {
            "replicas": 3,
            "selector": {"matchLabels": {"app": "web"}},
            "strategy": {"type": "RollingUpdate"},
            "template": {
                "metadata": {
                    "labels": {"app": "web", "pod-template-hash": "abc"},
                    "annotations": {"kubectl.kubernetes.io/restartedAt": "secret-ish-value"},
                },
                "spec": {
                    "serviceAccountName": "web-sa",
                    "nodeSelector": {"disk": "ssd"},
                    "tolerations": [
                        {
                            "key": "dedicated",
                            "operator": "Equal",
                            "value": "apps",
                            "effect": "NoSchedule",
                        }
                    ],
                    "affinity": {"podAntiAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": []}},
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
                                {
                                    "name": "PASSWORD",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "web-secret",
                                            "key": "password",
                                        }
                                    },
                                },
                            ],
                            "envFrom": [{"configMapRef": {"name": "web-config"}}],
                        }
                    ],
                    "initContainers": [
                        {
                            "name": "init",
                            "image": "registry.example.com/init:v1",
                            "resources": {"requests": {"cpu": "100m"}},
                        }
                    ],
                },
            },
        },
        "status": {
            "replicas": 3,
            "updatedReplicas": 3,
            "readyReplicas": 2,
            "availableReplicas": 2,
            "unavailableReplicas": 1,
            "observedGeneration": 9,
            "conditions": [
                {
                    "type": "Available",
                    "status": "False",
                    "reason": "MinimumReplicasUnavailable",
                    "message": "Deployment does not have minimum availability.",
                    "lastUpdateTime": "2026-01-01T00:01:00Z",
                    "lastTransitionTime": "2026-01-01T00:01:00Z",
                }
            ],
        },
    }


def _run_get_deployment(**kwargs):
    token = set_runner(FakeDeploymentRunner(_deployment()))
    try:
        return get_deployment("apps", "web", **kwargs)
    finally:
        runner_ctx.reset(token)


def test_get_deployment_default_exposes_safe_pod_template_contract():
    result = _run_get_deployment()

    assert result["name"] == "web"
    assert result["namespace"] == "apps"
    assert result["labels"] == {"app": "web", "tier": "frontend"}
    assert result["annotations"] == {
        "count": 2,
        "keys": ["checksum/config", "deployment.kubernetes.io/revision"],
    }
    assert result["revision"] == "7"
    assert result["generation"] == 9
    assert result["observed_generation"] == 9
    assert result["replicas"]["ready"] == 2
    assert result["health_status"] == "unhealthy"
    assert result["diagnostic_hint"] == "1 replica(s) unavailable"
    assert result["pod_template"]["service_account_name"] == "web-sa"
    assert result["pod_template"]["node_selector"] == {"disk": "ssd"}
    assert result["pod_template"]["images"] == ["registry.example.com/web:v1"]
    assert result["pod_template"]["volumes"][1] == {
        "name": "token",
        "type": "secret",
        "ref_name": "web-secret",
        "path": "",
    }


def test_get_deployment_labels_only_filters_to_labels_and_selector():
    result = _run_get_deployment(labels_only=True)

    assert result == {
        "name": "web",
        "namespace": "apps",
        "focused_modes": ["labels"],
        "labels": {"app": "web", "tier": "frontend"},
        "label_count": 2,
        "selector": {"matchLabels": {"app": "web"}},
        "pod_template": {
            "labels": {"app": "web", "pod-template-hash": "abc"},
            "label_count": 2,
        },
    }


def test_get_deployment_images_only_filters_to_container_images():
    result = _run_get_deployment(images_only=True)

    assert result["focused_modes"] == ["images"]
    assert result["images"] == ["registry.example.com/web:v1"]
    assert result["containers"] == [{"name": "app", "image": "registry.example.com/web:v1"}]
    assert result["init_containers"] == [{"name": "init", "image": "registry.example.com/init:v1"}]


def test_get_deployment_resources_only_filters_to_requests_and_limits():
    result = _run_get_deployment(resources_only=True)

    assert result["focused_modes"] == ["resources"]
    assert result["containers"] == [{
        "name": "app",
        "resources": {
            "requests": {"cpu": "500m", "memory": "256Mi"},
            "limits": {"cpu": "1", "memory": "1Gi"},
        },
    }]
    assert result["init_containers"] == [{
        "name": "init",
        "resources": {"requests": {"cpu": "100m"}},
    }]


def test_get_deployment_template_only_preserves_safe_template_details():
    result = _run_get_deployment(template_only=True)

    template = result["pod_template"]
    assert result["focused_modes"] == ["template"]
    assert result["selector"] == {"matchLabels": {"app": "web"}}
    assert template["service_account_name"] == "web-sa"
    assert template["tolerations"][0]["key"] == "dedicated"
    assert template["affinity"] == {
        "has_node_affinity": False,
        "has_pod_affinity": False,
        "has_pod_anti_affinity": True,
    }
    assert template["annotations"] == {
        "count": 1,
        "keys": ["kubectl.kubernetes.io/restartedAt"],
    }


def test_get_deployment_safe_template_omits_literal_env_and_annotation_values():
    result = _run_get_deployment()
    container = result["pod_template"]["containers"][0]
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
    assert "prod" not in str(result["pod_template"]["containers"])
    assert "secret-ish-value" not in str(result["pod_template"])
