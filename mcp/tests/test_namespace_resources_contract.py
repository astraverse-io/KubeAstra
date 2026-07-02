"""Regression tests for namespace resource inventory contracts."""

from pathlib import Path
import sys

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from k8s.kubectl_runner import runner_ctx, set_runner  # noqa: E402
from k8s.wrappers import list_namespace_resources  # noqa: E402


class FakeNamespaceRunner:
    def __init__(self, resources):
        self.resources = resources

    def run_json(self, args, namespace=None):
        assert args[:2] == ["get", args[1]]
        assert args[2:] == ["-o", "json"]
        assert namespace == "apps"
        return {"items": self.resources.get(args[1], [])}


def _workload(kind_name):
    return {
        "metadata": {
            "name": kind_name,
            "namespace": "apps",
            "labels": {"app": kind_name, "tier": "backend"},
        },
        "spec": {
            "replicas": 3,
            "serviceName": f"{kind_name}-headless",
            "selector": {"matchLabels": {"app": kind_name}},
            "template": {
                "metadata": {"labels": {"app": kind_name}},
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": f"registry.example.com/{kind_name}:v1",
                            "resources": {"requests": {"cpu": "250m"}},
                        }
                    ]
                },
            },
        },
        "status": {
            "readyReplicas": 2,
            "availableReplicas": 2,
            "updatedReplicas": 2,
            "unavailableReplicas": 1,
            "currentReplicas": 2,
            "desiredNumberScheduled": 3,
            "numberReady": 2,
            "numberAvailable": 2,
            "updatedNumberScheduled": 2,
        },
    }


def _resources():
    return {
        "pods": [
            {
                "metadata": {
                    "name": "web-0",
                    "namespace": "apps",
                    "labels": {"app": "web"},
                },
                "spec": {
                    "nodeName": "node-a",
                    "containers": [{"name": "app", "image": "registry.example.com/web:v1"}],
                },
                "status": {
                    "phase": "Running",
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "containerStatuses": [{"restartCount": 1}],
                },
            }
        ],
        "services": [
            {
                "metadata": {
                    "name": "web",
                    "namespace": "apps",
                    "labels": {"app": "web"},
                },
                "spec": {
                    "type": "ClusterIP",
                    "clusterIP": "10.0.0.1",
                    "selector": {"app": "web"},
                    "ports": [
                        {"name": "http", "port": 80, "targetPort": 8080, "protocol": "TCP"}
                    ],
                },
            }
        ],
        "deployments": [_workload("web")],
        "statefulsets": [_workload("db")],
        "daemonsets": [_workload("agent")],
        "configmaps": [
            {
                "metadata": {
                    "name": "app-config",
                    "namespace": "apps",
                    "labels": {"app": "web"},
                },
                "data": {"password": "do-not-expose"},
            },
            {"metadata": {"name": "kube-root-ca.crt", "namespace": "apps"}},
        ],
        "persistentvolumeclaims": [
            {
                "metadata": {
                    "name": "data-web-0",
                    "namespace": "apps",
                    "labels": {"app": "web"},
                },
                "spec": {
                    "storageClassName": "fast",
                    "accessModes": ["ReadWriteOnce"],
                    "volumeName": "pvc-123",
                },
                "status": {"phase": "Bound", "capacity": {"storage": "10Gi"}},
            }
        ],
        "ingresses": [
            {
                "metadata": {
                    "name": "web",
                    "namespace": "apps",
                    "labels": {"app": "web"},
                },
                "spec": {
                    "rules": [
                        {
                            "host": "web.example.com",
                            "http": {
                                "paths": [
                                    {
                                        "path": "/",
                                        "pathType": "Prefix",
                                        "backend": {
                                            "service": {
                                                "name": "web",
                                                "port": {"number": 80},
                                            }
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                },
            }
        ],
    }


def test_list_namespace_resources_exposes_safe_inventory_fields():
    token = set_runner(FakeNamespaceRunner(_resources()))
    try:
        result = list_namespace_resources("apps")
    finally:
        runner_ctx.reset(token)

    assert result["summary"] == {
        "pods": 1,
        "services": 1,
        "deployments": 1,
        "statefulsets": 1,
        "daemonsets": 1,
        "configmaps": 1,
        "persistent_volume_claims": 1,
        "ingresses": 1,
    }
    assert result["pods"][0]["labels"] == {"app": "web"}
    assert result["pods"][0]["images"] == ["registry.example.com/web:v1"]
    assert result["pods"][0]["node_name"] == "node-a"
    assert result["services"][0]["selector"] == {"app": "web"}
    assert result["services"][0]["ports"] == [{
        "name": "http",
        "protocol": "TCP",
        "port": 80,
        "target_port": 8080,
        "app_protocol": "",
    }]
    assert result["services"][0]["port_details"] == [{
        "name": "http",
        "protocol": "TCP",
        "port": 80,
        "target_port": 8080,
    }]
    assert result["deployments"][0]["selector"] == {"matchLabels": {"app": "web"}}
    assert result["deployments"][0]["images"] == ["registry.example.com/web:v1"]
    assert result["deployments"][0]["containers"][0]["resources"] == {"requests": {"cpu": "250m"}}
    assert result["statefulsets"][0]["service_name"] == "db-headless"
    assert result["daemonsets"][0]["desired"] == 3


def test_list_namespace_resources_includes_pvcs_and_ingress_backends_without_configmap_data():
    token = set_runner(FakeNamespaceRunner(_resources()))
    try:
        result = list_namespace_resources("apps")
    finally:
        runner_ctx.reset(token)

    assert result["configmaps"] == [{
        "name": "app-config",
        "namespace": "apps",
        "labels": {"app": "web"},
        "label_count": 1,
    }]
    assert "do-not-expose" not in str(result)
    assert result["persistent_volume_claims"] == [{
        "name": "data-web-0",
        "namespace": "apps",
        "labels": {"app": "web"},
        "label_count": 1,
        "status": "Bound",
        "storage_class_name": "fast",
        "access_modes": ["ReadWriteOnce"],
        "capacity": {"storage": "10Gi"},
        "volume_name": "pvc-123",
    }]
    assert result["ingresses"][0]["rules"] == [{
        "host": "web.example.com",
        "paths": [{
            "path": "/",
            "path_type": "Prefix",
            "backend": {"service": "web", "port": 80},
        }],
    }]
