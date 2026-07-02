"""Regression tests for pod investigation output contracts."""

from pathlib import Path
import sys

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from k8s.kubectl_runner import KubectlError, runner_ctx, set_runner  # noqa: E402
import k8s.wrappers as wrappers  # noqa: E402
from k8s.wrappers import investigate_pod  # noqa: E402


class Result:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""
        self.success = True
        self.truncated = False

    def raise_for_status(self):
        return None


class FakePodInvestigationRunner:
    def __init__(self, pod):
        self.pod = pod

    def run_json(self, args, namespace=None):
        assert namespace == "apps"
        if args == ["get", "pod", "web-0", "-o", "json"]:
            return self.pod
        if args[:3] == ["get", "events", "-o"]:
            return {"items": []}
        raise AssertionError(f"unexpected kubectl args: {args}")

    def run(self, args, namespace=None, **kwargs):
        assert namespace == "apps"
        if args == ["describe", "pod", "web-0"]:
            return Result("Name: web-0\nStatus: Running\n")
        if args[:2] == ["logs", "web-0"]:
            return Result("Check if Zookeeper is healthy\n")
        raise AssertionError(f"unexpected kubectl args: {args}")


class FakeMultiContainerRunner:
    def __init__(self, pod):
        self.pod = pod

    def run_json(self, args, namespace=None):
        assert namespace == "apps"
        if args == ["get", "pod", "kafka-0", "-o", "json"]:
            return self.pod
        if args[:3] == ["get", "events", "-o"]:
            return {"items": []}
        raise AssertionError(f"unexpected kubectl args: {args}")

    def run(self, args, namespace=None, **kwargs):
        assert namespace == "apps"
        if args == ["describe", "pod", "kafka-0"]:
            return Result("Name: kafka-0\nStatus: Running\n")
        if args[:2] == ["logs", "kafka-0"]:
            container = args[args.index("-c") + 1] if "-c" in args else ""
            previous = "--previous" in args
            if container == "kafka-broker":
                text = "Check if Zookeeper is healthy\n"
            elif container == "prometheus-jmx-exporter":
                text = "Error: Unable to access jarfile /opt/jmx_exporter/jmx_prometheus_javaagent.jar\n"
            else:
                text = ""
            return Result(("previous " if previous else "") + text)
        raise AssertionError(f"unexpected kubectl args: {args}")


class FakeLogFailureRunner(FakeMultiContainerRunner):
    def run(self, args, namespace=None, **kwargs):
        assert namespace == "apps"
        if args == ["describe", "pod", "kafka-0"]:
            return Result("Name: kafka-0\nStatus: Running\n")
        if args[:2] == ["logs", "kafka-0"]:
            container = args[args.index("-c") + 1] if "-c" in args else ""
            previous = "--previous" in args
            if container == "prometheus-jmx-exporter" and previous:
                raise KubectlError("previous logs unavailable", 1, "previous logs unavailable")
            return Result(f"{container} logs\n")
        raise AssertionError(f"unexpected kubectl args: {args}")


class FakeJenkinsPluginRunner:
    plugin_error = """
Multiple plugin prerequisites not met:
workflow-aggregator:608.v67378e9d3db_1 depends on pipeline-model-api:2.2291.v2934911987b_6,
but there is an older version defined on the top level - pipeline-model-api:2.2277.v00573e73ddf1
workflow-aggregator:608.v67378e9d3db_1 depends on pipeline-stage-tags-metadata:2.2291.v2934911987b_6,
but there is an older version defined on the top level - pipeline-stage-tags-metadata:2.2277.v00573e73ddf1
"""

    def run_json(self, args, namespace=None):
        assert namespace == "jenkins"
        if args == ["get", "pod", "jenkins-0", "-o", "json"]:
            return _jenkins_plugin_crash_pod()
        if args[:3] == ["get", "events", "-o"]:
            return {"items": []}
        raise AssertionError(f"unexpected kubectl args: {args}")

    def run(self, args, namespace=None, **kwargs):
        assert namespace == "jenkins"
        if args == ["describe", "pod", "jenkins-0"]:
            return Result("Name: jenkins-0\nStatus: Init:CrashLoopBackOff\n")
        if args[:2] == ["logs", "jenkins-0"]:
            previous = "--previous" in args
            if previous:
                return Result(self.plugin_error)
            return Result("Will install new plugin workflow-job 1571.1580.v18e46842c125\n")
        raise AssertionError(f"unexpected kubectl args: {args}")


class FakeFindWorkloadInitCrashRunner:
    def run_json(self, args, namespace=None):
        if args == ["get", "deployments", "--all-namespaces", "-o", "json"]:
            return {"items": []}
        if args == ["get", "services", "--all-namespaces", "-o", "json"]:
            return {"items": []}
        if args == ["get", "pods", "--all-namespaces", "-o", "json"]:
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "jenkins-legacy-0",
                            "namespace": "jenkins-legacy",
                        },
                        "spec": {
                            "containers": [{"name": "jenkins", "image": "jenkins:v1"}],
                        },
                        "status": {
                            "phase": "Pending",
                            "initContainerStatuses": [
                                {
                                    "name": "init",
                                    "restartCount": 12,
                                    "state": {
                                        "waiting": {
                                            "reason": "CrashLoopBackOff",
                                            "message": "back-off restarting failed init container",
                                        }
                                    },
                                }
                            ],
                            "containerStatuses": [
                                {
                                    "name": "jenkins",
                                    "ready": False,
                                    "restartCount": 0,
                                    "state": {"waiting": {"reason": "PodInitializing"}},
                                }
                            ],
                        },
                    }
                ]
            }
        raise AssertionError(f"unexpected kubectl args: {args}")


def _crashing_pod():
    return {
        "metadata": {
            "name": "web-0",
            "namespace": "apps",
            "labels": {"app": "web"},
            "ownerReferences": [{"kind": "ReplicaSet", "name": "web-abc", "controller": True}],
        },
        "spec": {
            "nodeName": "node-a",
            "serviceAccountName": "web-sa",
            "nodeSelector": {"disk": "ssd"},
            "tolerations": [{"key": "dedicated", "operator": "Exists", "effect": "NoSchedule"}],
            "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {}}},
            "volumes": [
                {"name": "settings", "configMap": {"name": "web-config"}},
                {"name": "secret", "secret": {"secretName": "web-secret"}},
            ],
            "containers": [
                {
                    "name": "app",
                    "image": "registry.example.com/web:v1",
                    "resources": {"requests": {"cpu": "500m"}},
                    "env": [
                        {"name": "PUBLIC_MODE", "value": "prod"},
                        {
                            "name": "PASSWORD",
                            "valueFrom": {"secretKeyRef": {"name": "web-secret", "key": "password"}},
                        },
                    ],
                    "envFrom": [{"configMapRef": {"name": "web-config"}}],
                }
            ],
        },
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": "app",
                    "ready": False,
                    "restartCount": 6,
                    "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off"}},
                }
            ],
        },
    }


def _multi_container_crashing_pod():
    return {
        "metadata": {"name": "kafka-0", "namespace": "apps", "labels": {"app": "kafka"}},
        "spec": {
            "nodeName": "node-a",
            "containers": [
                {"name": "kafka-broker", "image": "kafka:v1", "env": []},
                {"name": "prometheus-jmx-exporter", "image": "jmx:v1", "env": []},
            ],
        },
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": "kafka-broker",
                    "ready": False,
                    "restartCount": 6,
                    "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off"}},
                },
                {
                    "name": "prometheus-jmx-exporter",
                    "ready": False,
                    "restartCount": 6,
                    "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off"}},
                    "lastState": {"terminated": {"reason": "Error", "exitCode": 1}},
                },
            ],
        },
    }


def _jenkins_plugin_crash_pod():
    return {
        "metadata": {"name": "jenkins-0", "namespace": "jenkins", "labels": {"app": "jenkins"}},
        "spec": {
            "nodeName": "node-a",
            "initContainers": [{"name": "init", "image": "jenkins/init:v1", "env": []}],
            "containers": [
                {"name": "config-reload", "image": "config-reload:v1", "env": []},
                {"name": "jenkins", "image": "jenkins:v1", "env": []},
            ],
        },
        "status": {
            "phase": "Pending",
            "initContainerStatuses": [
                {
                    "name": "init",
                    "ready": False,
                    "restartCount": 7,
                    "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off"}},
                    "lastState": {"terminated": {"reason": "Error", "exitCode": 1}},
                }
            ],
            "containerStatuses": [
                {
                    "name": "config-reload",
                    "ready": False,
                    "restartCount": 0,
                    "state": {"waiting": {"reason": "PodInitializing"}},
                },
                {
                    "name": "jenkins",
                    "ready": False,
                    "restartCount": 0,
                    "state": {"waiting": {"reason": "PodInitializing"}},
                },
            ],
        },
    }


def test_investigate_pod_includes_safe_pod_spec_summary_without_literal_env_values():
    token = set_runner(FakePodInvestigationRunner(_crashing_pod()))
    try:
        result = investigate_pod("apps", "web-0", use_ai=False)
    finally:
        runner_ctx.reset(token)

    summary = result["pod_spec_summary"]
    assert result["classification"]["mode"] == "CrashLoopBackOff"
    assert summary["labels"] == {"app": "web"}
    assert summary["owner_references"] == [{"kind": "ReplicaSet", "name": "web-abc", "controller": True}]
    assert summary["service_account_name"] == "web-sa"
    assert summary["node_name"] == "node-a"
    assert summary["node_selector"] == {"disk": "ssd"}
    assert summary["affinity"]["has_node_affinity"] is True
    assert summary["images"] == ["registry.example.com/web:v1"]
    assert summary["volumes"][1] == {
        "name": "secret",
        "type": "secret",
        "ref_name": "web-secret",
        "path": "",
    }
    container = summary["containers"][0]
    literal_env = next(env for env in container["env"] if env["name"] == "PUBLIC_MODE")
    secret_env = next(env for env in container["env"] if env["name"] == "PASSWORD")
    assert literal_env == {"name": "PUBLIC_MODE", "value_from": None, "has_literal_value": True}
    assert secret_env["value_from"]["type"] == "secretKeyRef"
    assert "prod" not in str(summary["containers"])
    assert len(result["container_log_findings"]) == 1
    assert result["container_log_findings"][0]["container"] == "app"
    assert result["evidence_summary"]["secondary_issues"] == []


def test_investigate_pod_collects_findings_for_all_failing_containers():
    token = set_runner(FakeMultiContainerRunner(_multi_container_crashing_pod()))
    try:
        result = investigate_pod("apps", "kafka-0", use_ai=False)
    finally:
        runner_ctx.reset(token)

    findings = result["container_log_findings"]
    containers = {finding["container"]: finding for finding in findings}

    assert result["classification"]["container"] == "kafka-broker"
    assert "kafka-broker" in containers
    assert "prometheus-jmx-exporter" in containers
    assert "Zookeeper is healthy" in containers["kafka-broker"]["logs_previous"]["excerpt"]
    assert "Unable to access jarfile" in containers["prometheus-jmx-exporter"]["logs_previous"]["excerpt"]
    assert result["evidence_summary"]["secondary_issues"][0]["container"] == "prometheus-jmx-exporter"


def test_investigate_pod_keeps_container_finding_when_one_log_fetch_fails():
    token = set_runner(FakeLogFailureRunner(_multi_container_crashing_pod()))
    try:
        result = investigate_pod("apps", "kafka-0", use_ai=False)
    finally:
        runner_ctx.reset(token)

    containers = {finding["container"]: finding for finding in result["container_log_findings"]}

    assert result["success"] is True
    assert "prometheus-jmx-exporter" in containers
    prometheus = containers["prometheus-jmx-exporter"]
    assert prometheus["logs_current"]["success"] is True
    assert prometheus["logs_previous"]["success"] is False
    assert "previous logs unavailable" in prometheus["logs_previous"]["error"]


def test_investigate_pod_identifies_jenkins_plugin_dependency_mismatch():
    token = set_runner(FakeJenkinsPluginRunner())
    try:
        result = investigate_pod("jenkins", "jenkins-0", use_ai=False)
    finally:
        runner_ctx.reset(token)

    evidence = result["evidence_summary"]

    assert result["classification"]["container"] == "init"
    assert "application dependency resolution" in evidence["suspected_root_cause"]
    assert "pipeline-model-api:2.2291.v2934911987b_6" in evidence["suspected_root_cause"]
    assert "pipeline-model-api:2.2277.v00573e73ddf1" in evidence["suspected_root_cause"]
    assert "dependency pin list" in evidence["suggested_fix"]
    diagnostics = [
        item for item in evidence["evidence"]
        if isinstance(item, dict) and item.get("type") == "container_log_diagnostic"
    ]
    assert diagnostics
    assert diagnostics[0]["diagnostic"]["mismatches"][0] == {
        "required": "pipeline-model-api:2.2291.v2934911987b_6",
        "pinned": "pipeline-model-api:2.2277.v00573e73ddf1",
    }


def test_find_workload_derives_status_from_init_container_crash(monkeypatch):
    monkeypatch.setattr(wrappers, "get_allowed_namespaces", lambda: ["*"])
    token = set_runner(FakeFindWorkloadInitCrashRunner())
    try:
        result = wrappers.find_workload("jenkins-legacy-0")
    finally:
        runner_ctx.reset(token)

    assert result["pods"][0]["namespace"] == "jenkins-legacy"
    assert result["pods"][0]["status"] == "CrashLoopBackOff"
    assert result["pods"][0]["restarts"] == 12
