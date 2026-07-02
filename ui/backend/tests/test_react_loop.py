"""Regression tests for ReAct loop reliability behavior."""

from pathlib import Path
import sys
import time

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from react import _truncate_observation, react_loop  # noqa: E402


class SequencedProvider:
    def __init__(self, responses):
        self.responses = list(responses)

    def generate_stream(self, prompt, system=None, temperature=0.1, max_tokens=8000):
        assert self.responses, "provider called more times than expected"
        response = self.responses.pop(0)
        for char in response:
            yield char


def test_react_deadline_stops_after_bounded_tool_call():
    provider = SequencedProvider([
        '{"thought":"inspect","action":"get_pods","params":{"namespace":"default"}}',
    ])

    def slow_dispatch(tool, params):
        time.sleep(0.03)
        return {"pods": []}

    started = time.perf_counter()
    result = react_loop(
        "check pods",
        [],
        provider,
        slow_dispatch,
        deadline_monotonic=started + 0.01,
    )

    assert result.error and "timed out" in result.error
    assert time.perf_counter() - started < 0.5


def test_react_observation_preserves_node_cpu_allocation_compactly():
    result = {
        "name": "node-a",
        "query": "node-a",
        "status": "Ready",
        "labels": {"large": "label-data"},
        "capacity": {"cpu": "16", "cpu_millicores": 16000, "memory_gib": 31.0},
        "allocatable": {"cpu": "16", "cpu_millicores": 16000, "memory_gib": 30.9},
        "allocated": {
            "cpu_requests_millicores": 300,
            "cpu_requests_cores": 0.3,
            "cpu_requests_percent_of_allocatable": 1.88,
            "cpu_limits_millicores": 150,
            "cpu_limits_cores": 0.15,
            "cpu_limits_percent_of_allocatable": 0.94,
            "non_terminated_pods": 6,
        },
        "pods": [{"name": f"pod-{i}", "cpu_requests_millicores": i} for i in range(25)],
    }

    text = _truncate_observation(result, "investigate_node")

    assert '"cpu_requests_cores": 0.3' in text
    assert '"cpu_limits_cores": 0.15' in text
    assert '"non_terminated_pods": 6' in text
    assert "large" not in text
    assert "pod-19" in text
    assert "pod-20" not in text


def test_react_observation_preserves_container_log_findings_for_pod_investigation():
    result = {
        "pod_name": "kafka-0",
        "namespace": "infrastructure",
        "classification": {"mode": "CrashLoopBackOff", "container": "kafka-broker"},
        "evidence_summary": {
            "suspected_root_cause": "Kafka cannot reach ZooKeeper.",
        },
        "container_log_findings": [
            {
                "container": "prometheus-jmx-exporter",
                "reason": "CrashLoopBackOff",
                "restart_count": 6,
                "logs_previous": {
                    "excerpt": "Error: Unable to access jarfile /opt/jmx_exporter/jmx_prometheus_javaagent.jar",
                },
            }
        ],
    }

    text = _truncate_observation(result, "investigate_pod")

    assert '"container_log_findings"' in text
    assert "prometheus-jmx-exporter" in text
    assert "Unable to access jarfile" in text


def test_react_recovers_after_unknown_tool_error():
    provider = SequencedProvider([
        '{"thought":"Need node data","action":"investigate_nodes","params":{"node_name":"node-a"}}',
        '{"thought":"The previous tool was invalid; I can answer from the corrected evidence.","action":"answer","answer":"draft"}',
        "final answer",
    ])

    calls = []

    def dispatch_fn(tool, params):
        calls.append((tool, params))
        return {
            "error": "unknown_tool",
            "message": f"Unknown tool: {tool}",
            "tool": tool,
            "valid_tools": ["investigate_node", "get_nodes"],
        }

    result = react_loop(
        question="what CPU is allocated on node-a",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=3,
    )

    assert calls == [("investigate_nodes", {"node_name": "node-a"})]
    assert result.answer == "final answer"
    assert result.error is None
    assert [step.action for step in result.steps] == ["investigate_nodes", "answer"]


def test_react_sanitizes_answer_action_fallback_when_finalize_returns_empty():
    provider = SequencedProvider([
        '{"thought":"List pods","action":"get_pods","params":{"namespace":"apps"}}',
        (
            '{"thought":"I can answer from the inventory","action":"answer",'
            '"answer":"# Evidence\\n* envelope[0].evidence.items[0] - api-0 is CrashLoopBackOff"}'
        ),
    ])

    def dispatch_fn(tool, params):
        return {
            "items": [{"name": "api-0", "namespace": "apps", "status": "CrashLoopBackOff"}],
            "total_count": 1,
        }

    result = react_loop(
        question="why is api crashing?",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=3,
    )

    assert "api-0 is CrashLoopBackOff" in result.answer
    assert "envelope[" not in result.answer
    assert "envelope[" not in str(result.synthesis_breakdown)


def test_react_loop_duplicate_tool_call_prevention_and_recovery():
    provider = SequencedProvider([
        '{"thought":"Let me check the pods","action":"get_pods","params":{"namespace":"default"}}',
        '{"thought":"Let me check the pods again just in case","action":"get_pods","params":{"namespace":"default"}}',
        '{"thought":"I see a duplicate error. I will now answer.","action":"answer","answer":"completed answer"}',
        "final answer body",
    ])

    calls = []

    def dispatch_fn(tool, params):
        calls.append((tool, params))
        return {
            "pods": [{"name": "pod-1", "status": "Running"}]
        }

    result = react_loop(
        question="list pods",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=4,
    )

    # dispatch_fn should ONLY be called once because the second call was intercepted as a duplicate!
    assert calls == [("get_pods", {"namespace": "default"})]
    assert result.answer == "final answer body"
    assert result.error is None

    # Let's inspect steps to verify
    assert len(result.steps) == 3
    assert result.steps[0].action == "get_pods"
    assert result.steps[1].action == "get_pods"
    assert "duplicate_tool_call" in result.steps[1].observation
    assert result.steps[2].action == "answer"


def test_react_coerces_all_node_labels_away_from_stale_investigate_node():
    provider = SequencedProvider([
        '{"thought":"I will reuse the prior node","action":"investigate_node","params":{"node_name":"k8s-worker-01"}}',
        '{"thought":"I have the node inventory and labels now","action":"answer","answer":"done"}',
        "node labels answer",
    ])

    calls = []

    def dispatch_fn(tool, params):
        calls.append((tool, params))
        return {
            "node_count": 1,
            "labels_only": True,
            "nodes": [
                {
                    "name": "k8s-worker-01",
                    "labels": {"node-role.kubernetes.io/worker": "", "zone": "east"},
                    "label_count": 2,
                }
            ],
        }

    result = react_loop(
        question="get all node labels for all nodes",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=3,
    )

    assert calls == [("get_nodes", {"labels_only": True})]
    assert result.tool_used == "get_nodes"
    assert result.result["nodes"][0]["labels"]["zone"] == "east"
    assert result.steps[0].action == "get_nodes"


def test_react_filters_plain_get_nodes_for_label_only_question():
    provider = SequencedProvider([
        '{"thought":"I will list nodes","action":"get_nodes","params":{}}',
        '{"thought":"I have the labels now","action":"answer","answer":"done"}',
        "node labels answer",
    ])

    calls = []

    def dispatch_fn(tool, params):
        calls.append((tool, params))
        return {"node_count": 0, "labels_only": True, "nodes": []}

    result = react_loop(
        question="show labels for all nodes",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=3,
    )

    assert calls == [("get_nodes", {"labels_only": True})]
    assert result.result["labels_only"] is True


def test_react_keeps_specific_node_label_investigation():
    provider = SequencedProvider([
        '{"thought":"This is about one node","action":"investigate_node","params":{"node_name":"node-a"}}',
        '{"thought":"I have the specific node details","action":"answer","answer":"done"}',
        "specific node answer",
    ])

    calls = []

    def dispatch_fn(tool, params):
        calls.append((tool, params))
        return {"name": "node-a", "labels": {"zone": "east"}, "label_count": 1}

    result = react_loop(
        question="get labels for node node-a",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=3,
    )

    assert calls == [("investigate_node", {"node_name": "node-a"})]
    assert result.tool_used == "investigate_node"


def test_react_coerces_all_node_taints_to_get_nodes_focused_mode():
    provider = SequencedProvider([
        '{"thought":"I will inspect the previous node","action":"investigate_node","params":{"node_name":"node-a"}}',
        '{"thought":"I have the taints now","action":"answer","answer":"done"}',
        "node taints answer",
    ])

    calls = []

    def dispatch_fn(tool, params):
        calls.append((tool, params))
        return {
            "node_count": 1,
            "focused_modes": ["taints"],
            "nodes": [{"name": "node-a", "taints": [], "unschedulable": False}],
        }

    result = react_loop(
        question="show taints for all nodes",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=3,
    )

    assert calls == [("get_nodes", {"taints_only": True})]
    assert result.tool_used == "get_nodes"
    assert result.result["focused_modes"] == ["taints"]


def test_react_adds_images_only_for_focused_pod_inventory():
    provider = SequencedProvider([
        '{"thought":"I will list pods","action":"get_pods","params":{}}',
        '{"thought":"I have the images now","action":"answer","answer":"done"}',
        "pod images answer",
    ])

    calls = []

    def dispatch_fn(tool, params):
        calls.append((tool, params))
        return {
            "namespace": "*",
            "pod_count": 1,
            "focused_modes": ["images"],
            "pods": [{
                "namespace": "apps",
                "name": "web-0",
                "images": ["registry.example.com/web:v1"],
                "containers": [{"name": "app", "image": "registry.example.com/web:v1"}],
            }],
        }

    result = react_loop(
        question="what pod images are running",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=3,
    )

    assert calls == [("get_pods", {"images_only": True, "namespace": "*"})]
    assert result.tool_used == "get_pods"
    assert result.result["focused_modes"] == ["images"]


def test_react_coerces_simple_crashloop_question_to_filtered_pod_inventory():
    provider = SequencedProvider([
        '{"thought":"I should investigate one crashing pod","action":"investigate_pod","params":{"namespace":"infrastructure","pod_name":"my-kafka-0"}}',
        '{"thought":"I have the CrashLoopBackOff pod list now","action":"answer","answer":"done"}',
        "crashloop pod list answer",
    ])

    calls = []

    def dispatch_fn(tool, params):
        calls.append((tool, params))
        return {
            "namespace": "*",
            "status_filter": "CrashLoopBackOff",
            "pod_count": 1,
            "pods": [{"namespace": "infrastructure", "name": "my-kafka-0", "status": "CrashLoopBackOff"}],
        }

    result = react_loop(
        question="any pods in crashloop status",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=3,
    )

    assert calls == [("get_pods", {"namespace": "*", "status_filter": "CrashLoopBackOff"})]
    assert result.tool_used == "get_pods"
    assert result.result["status_filter"] == "CrashLoopBackOff"


def test_react_preserves_investigation_result_when_followup_inventory_runs():
    provider = SequencedProvider([
        '{"thought":"I will investigate the Kafka pod","action":"investigate_pod","params":{"namespace":"infrastructure","pod_name":"my-kafka-0","use_ai":true}}',
        '{"thought":"I will check related namespace resources","action":"list_namespace_resources","params":{"namespace":"infrastructure"}}',
        '{"thought":"I have enough evidence","action":"answer","answer":"done"}',
        "root cause answer",
    ])

    calls = []

    def dispatch_fn(tool, params):
        calls.append((tool, params))
        if tool == "investigate_pod":
            return {
                "pod_name": "my-kafka-0",
                "namespace": "infrastructure",
                "evidence_summary": {
                    "suspected_root_cause": "Kafka cannot connect to ZooKeeper service.",
                    "suggested_fix": "Restore the missing ZooKeeper service or update KAFKA_ZOOKEEPER_CONNECT.",
                },
            }
        return {
            "namespace": "infrastructure",
            "summary": {"pods": 4, "services": 3},
        }

    result = react_loop(
        question="can you help me identify why kafka pods are in crashlopp status",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=3,
    )

    assert calls == [
        ("investigate_pod", {"namespace": "infrastructure", "pod_name": "my-kafka-0", "use_ai": True}),
        ("list_namespace_resources", {"namespace": "infrastructure"}),
    ]
    assert result.tool_used == "investigate_pod"
    assert result.result["evidence_summary"]["suspected_root_cause"] == "Kafka cannot connect to ZooKeeper service."
    assert result.result["root_cause_summary"]["schema_version"] == "root_cause_summary.v1"
    assert result.result["root_cause_summary"]["root_cause"] == "Kafka cannot connect to ZooKeeper service."
    assert result.synthesis_breakdown["root_cause_summary"]["resource_name"] == "my-kafka-0"
    assert result.result["root_cause_summary"]["root_cause"] in result.answer


def test_react_coerces_kafka_crashloop_prompt_away_from_dependency_inventory_first():
    provider = SequencedProvider([
        (
            '{"thought":"The user is asking about the same Kafka pods. I should verify ZooKeeper endpoints.",'
            '"action":"get_endpoints","params":{"namespace":"infrastructure","service_name":"zookeeper"}}'
        ),
        '{"thought":"I have the failing pod evidence now","action":"answer","answer":"done"}',
        "root cause answer",
    ])

    calls = []

    def dispatch_fn(tool, params):
        calls.append((tool, params))
        return {
            "pod_name": "my-kafka-0",
            "namespace": "infrastructure",
            "classification": {"mode": "CrashLoopBackOff"},
            "evidence_summary": {
                "suspected_root_cause": "Kafka cannot connect to ZooKeeper service.",
                "suggested_fix": "Restore the missing ZooKeeper service.",
            },
        }

    result = react_loop(
        question="can you help me identify why kafka pods are in crashlopp status",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=3,
    )

    assert calls == [("investigate_pod", {"pod_name": "kafka", "use_ai": True, "namespace": "infrastructure"})]
    assert result.tool_used == "investigate_pod"
    assert result.result["evidence_summary"]["suspected_root_cause"] == "Kafka cannot connect to ZooKeeper service."


def test_react_keeps_kafka_pod_result_when_later_dependency_investigation_runs():
    provider = SequencedProvider([
        '{"thought":"Investigate Kafka pod","action":"investigate_pod","params":{"namespace":"infrastructure","pod_name":"my-kafka-0","use_ai":true}}',
        '{"thought":"Fetch kafka logs","action":"get_pod_logs","params":{"namespace":"infrastructure","pod_name":"my-kafka-0","container":"kafka-broker","previous":true}}',
        '{"thought":"Check namespace resources","action":"list_namespace_resources","params":{"namespace":"infrastructure"}}',
        '{"thought":"Investigate ZooKeeper workload","action":"investigate_workload","params":{"namespace":"infrastructure","workload_name":"zookeeper","workload_type":"deployment","use_ai":true}}',
        '{"thought":"Find ZooKeeper workload","action":"find_workload","params":{"name":"zookeeper"}}',
        '{"thought":"I have enough evidence","action":"answer","answer":"done"}',
    ])

    calls = []

    def dispatch_fn(tool, params):
        calls.append((tool, params))
        if tool == "investigate_pod":
            return {
                "pod_name": "my-kafka-0",
                "namespace": "infrastructure",
                "classification": {"mode": "CrashLoopBackOff", "container": "kafka-broker"},
                "evidence_summary": {
                    "suspected_root_cause": "Kafka cannot connect to ZooKeeper service.",
                    "suggested_fix": "Restore the missing ZooKeeper service.",
                    "secondary_issues": [{
                        "container": "prometheus-jmx-exporter",
                        "evidence": "Error: Unable to access jarfile /opt/jmx_exporter/jmx_prometheus_javaagent.jar",
                    }],
                },
                "container_log_findings": [{
                    "container": "prometheus-jmx-exporter",
                    "reason": "CrashLoopBackOff",
                    "restart_count": 6,
                    "logs_previous": {
                        "excerpt": "Error: Unable to access jarfile /opt/jmx_exporter/jmx_prometheus_javaagent.jar",
                    },
                }],
            }
        if tool == "investigate_workload":
            return {
                "workload_name": "zookeeper",
                "namespace": "infrastructure",
                "workload_type": "deployment",
                "error": "not found",
            }
        return {"ok": True, "tool": tool}

    result = react_loop(
        question="can you help me identify why kafka pods are in crashloop",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=6,
    )

    assert [tool for tool, _ in calls] == [
        "investigate_pod",
        "get_pod_logs",
        "list_namespace_resources",
        "investigate_workload",
        "find_workload",
    ]
    assert result.tool_used == "investigate_pod"
    assert result.result["pod_name"] == "my-kafka-0"
    assert "Kafka cannot connect to ZooKeeper service" in result.answer
    assert "prometheus-jmx-exporter" in result.answer
    assert "Unable to access jarfile" in result.answer
    assert "resource" not in result.answer.lower()
    assert "patch:apply" not in result.answer


def test_react_reinvestigates_discovered_crashloop_pod_after_empty_first_attempt():
    provider = SequencedProvider([
        '{"thought":"I need to investigate my-kafka-0","action":"investigate_pod","params":{"pod_name":"my-kafka-0","use_ai":true}}',
        '{"thought":"The first investigation was empty; I should find the workload namespace","action":"find_workload","params":{"name":"my-kafka"}}',
        '{"thought":"I should list pods in infrastructure","action":"get_pods","params":{"namespace":"infrastructure","status_filter":"CrashLoopBackOff"}}',
        '{"thought":"I have enough status evidence","action":"answer","answer":"done"}',
        '{"thought":"I now have the concrete pod evidence","action":"answer","answer":"done"}',
        "final synthesized answer",
    ])

    calls = []

    def dispatch_fn(tool, params):
        calls.append((tool, params))
        if tool == "investigate_pod" and params.get("namespace") == "infrastructure":
            return {
                "pod_name": "my-kafka-0",
                "namespace": "infrastructure",
                "classification": {"mode": "CrashLoopBackOff", "container": "kafka-broker"},
                "evidence_summary": {
                    "suspected_root_cause": "Kafka cannot connect to ZooKeeper service.",
                    "suggested_fix": "Restore the missing ZooKeeper service.",
                },
            }
        if tool == "find_workload":
            return {
                "services": [{"namespace": "infrastructure", "name": "my-kafka"}],
                "pods": [],
            }
        if tool == "get_pods":
            return {
                "namespace": "infrastructure",
                "status_filter": "CrashLoopBackOff",
                "pod_count": 3,
                "pods": [
                    {"namespace": "infrastructure", "name": "my-kafka-0", "status": "Error"},
                    {"namespace": "infrastructure", "name": "my-kafka-1", "status": "CrashLoopBackOff"},
                    {"namespace": "infrastructure", "name": "my-kafka-2", "status": "CrashLoopBackOff"},
                ],
            }
        return {}

    result = react_loop(
        question="can you help me identify why kafka pods are in crashloop?",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=5,
    )

    assert calls == [
        ("investigate_pod", {"pod_name": "my-kafka-0", "use_ai": True}),
        ("find_workload", {"name": "my-kafka"}),
        ("get_pods", {"namespace": "infrastructure", "status_filter": "CrashLoopBackOff"}),
        ("investigate_pod", {"namespace": "infrastructure", "pod_name": "my-kafka-0", "use_ai": True}),
    ]
    assert result.tool_used == "investigate_pod"
    assert result.result["evidence_summary"]["suspected_root_cause"] == "Kafka cannot connect to ZooKeeper service."


def test_react_forces_pod_investigation_when_candidate_found_on_last_iteration():
    provider = SequencedProvider([
        '{"thought":"First try direct investigation","action":"investigate_pod","params":{"pod_name":"my-kafka-0","use_ai":true}}',
        '{"thought":"Search all pods as a final discovery step","action":"get_pods","params":{"namespace":"*","status_filter":"CrashLoopBackOff"}}',
    ])

    calls = []

    def dispatch_fn(tool, params):
        calls.append((tool, params))
        if tool == "get_pods":
            return {
                "namespace": "*",
                "status_filter": "CrashLoopBackOff",
                "pod_count": 1,
                "pods": [
                    {"namespace": "infrastructure", "name": "my-kafka-0", "status": "Error"},
                ],
            }
        if tool == "investigate_pod" and params.get("namespace") == "infrastructure":
            return {
                "pod_name": "my-kafka-0",
                "namespace": "infrastructure",
                "classification": {"mode": "CrashLoopBackOff", "container": "kafka-broker"},
                "evidence_summary": {
                    "suspected_root_cause": "Kafka cannot connect to ZooKeeper service.",
                    "suggested_fix": "Restore the missing ZooKeeper service.",
                },
            }
        return {}

    result = react_loop(
        question="can you help me identify why kafka pods are in crashloop?",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=2,
    )

    assert calls == [
        ("investigate_pod", {"pod_name": "my-kafka-0", "use_ai": True}),
        ("get_pods", {"namespace": "*", "status_filter": "CrashLoopBackOff"}),
        ("investigate_pod", {"namespace": "infrastructure", "pod_name": "my-kafka-0", "use_ai": True}),
    ]
    assert result.error is None
    assert result.tool_used == "investigate_pod"
    assert "Kafka cannot connect to ZooKeeper service" in result.answer


def test_react_forces_investigation_from_items_inventory_after_empty_initial_probe():
    provider = SequencedProvider([
        '{"thought":"I will investigate jenkins-legacy","action":"investigate_pod","params":{"pod_name":"jenkins-legacy","use_ai":true}}',
        '{"thought":"I need to find the namespace","action":"find_workload","params":{"name":"jenkins-legacy"}}',
        '{"thought":"I will list pods in the jenkins namespace","action":"get_pods","params":{"namespace":"jenkins-legacy"}}',
    ])

    calls = []

    def dispatch_fn(tool, params):
        calls.append((tool, params))
        if tool == "investigate_pod" and params.get("namespace") == "jenkins-legacy":
            return {
                "pod_name": "jenkins-legacy-0",
                "namespace": "jenkins-legacy",
                "classification": {"mode": "Pending", "container": "jenkins"},
                "evidence_summary": {
                    "suspected_root_cause": "Pod is blocked during init container startup.",
                    "evidence": ["Initialized=False", "PodInitializing"],
                },
                "container_log_findings": [{
                    "container": "init",
                    "reason": "PodInitializing",
                    "logs_previous": {"excerpt": "waiting for truststore generation"},
                }],
            }
        if tool == "find_workload":
            return {"services": [{"namespace": "jenkins-legacy", "name": "jenkins-legacy"}], "pods": []}
        if tool == "get_pods":
            return {
                "namespace": "jenkins-legacy",
                "status_filter": "CrashLoopBackOff",
                "total_count": 1,
                "items": [
                    {
                        "namespace": "jenkins-legacy",
                        "name": "jenkins-legacy-0",
                        "status": "Pending",
                    }
                ],
            }
        return {}

    result = react_loop(
        question="can you help me identify why jenkins-legacy pod is in crashloop?",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=3,
    )

    assert calls == [
        ("investigate_pod", {"pod_name": "jenkins-legacy", "use_ai": True}),
        ("find_workload", {"name": "jenkins-legacy"}),
        ("get_pods", {"namespace": "jenkins-legacy"}),
        ("investigate_pod", {"namespace": "jenkins-legacy", "pod_name": "jenkins-legacy-0", "use_ai": True}),
    ]
    assert result.error is None
    assert result.tool_used == "investigate_pod"
    assert result.result["pod_name"] == "jenkins-legacy-0"
    assert "blocked during init container startup" in result.answer
    assert "waiting for truststore generation" in result.answer


def test_react_max_iterations_still_answers_from_verified_pod_evidence():
    provider = SequencedProvider([
        '{"thought":"Investigate Kafka pod","action":"investigate_pod","params":{"namespace":"infrastructure","pod_name":"my-kafka-0","use_ai":true}}',
        '{"thought":"Fetch kafka logs","action":"get_pod_logs","params":{"namespace":"infrastructure","pod_name":"my-kafka-0","container":"kafka-broker","previous":true}}',
        '{"thought":"Check namespace resources","action":"list_namespace_resources","params":{"namespace":"infrastructure"}}',
    ])

    def dispatch_fn(tool, params):
        if tool == "investigate_pod":
            return {
                "pod_name": "my-kafka-0",
                "namespace": "infrastructure",
                "classification": {"mode": "CrashLoopBackOff", "container": "kafka-broker"},
                "evidence_summary": {
                    "suspected_root_cause": "Kafka cannot connect to ZooKeeper service.",
                    "suggested_fix": "Restore the missing ZooKeeper service.",
                },
                "container_log_findings": [{
                    "container": "prometheus-jmx-exporter",
                    "reason": "CrashLoopBackOff",
                    "restart_count": 6,
                    "logs_previous": {
                        "excerpt": "Error: Unable to access jarfile /opt/jmx_exporter/jmx_prometheus_javaagent.jar",
                    },
                }],
            }
        return {"ok": True, "tool": tool}

    result = react_loop(
        question="can you help me identify why kafka pods are in crashloop",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=3,
    )

    assert result.error is None
    assert result.tool_used == "investigate_pod"
    assert "Kafka cannot connect to ZooKeeper service" in result.answer
    assert "prometheus-jmx-exporter" in result.answer
    assert "Unable to access jarfile" in result.answer


def test_react_coerces_broad_pod_labels_away_from_single_pod_investigation():
    provider = SequencedProvider([
        '{"thought":"I will inspect the previous pod","action":"investigate_pod","params":{"namespace":"apps","pod_name":"web-0"}}',
        '{"thought":"I have the pod labels now","action":"answer","answer":"done"}',
        "pod labels answer",
    ])

    calls = []

    def dispatch_fn(tool, params):
        calls.append((tool, params))
        return {
            "namespace": "*",
            "pod_count": 1,
            "focused_modes": ["labels"],
            "pods": [{
                "namespace": "apps",
                "name": "web-0",
                "labels": {"app": "web"},
                "label_count": 1,
            }],
        }

    result = react_loop(
        question="show labels for all pods",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=3,
    )

    assert calls == [("get_pods", {"namespace": "*", "labels_only": True})]
    assert result.steps[0].action == "get_pods"
    assert result.result["focused_modes"] == ["labels"]


def test_react_adds_resources_only_for_focused_deployment_question():
    provider = SequencedProvider([
        '{"thought":"I will check the deployment","action":"get_deployment","params":{"namespace":"apps","deployment_name":"web"}}',
        '{"thought":"I have the deployment resources now","action":"answer","answer":"done"}',
        "deployment resources answer",
    ])

    calls = []

    def dispatch_fn(tool, params):
        calls.append((tool, params))
        return {
            "name": "web",
            "namespace": "apps",
            "focused_modes": ["resources"],
            "containers": [{
                "name": "app",
                "resources": {"requests": {"cpu": "500m"}},
            }],
        }

    result = react_loop(
        question="show resource requests and limits for deployment web in apps",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=3,
    )

    assert calls == [(
        "get_deployment",
        {"namespace": "apps", "deployment_name": "web", "resources_only": True},
    )]
    assert result.tool_used == "get_deployment"
    assert result.result["focused_modes"] == ["resources"]


def test_react_loop_cancellation():
    provider = SequencedProvider([
        '{"thought":"Let me check the nodes","action":"get_nodes","params":{}}',
        '{"thought":"I will answer now","action":"answer","answer":"finished"}',
        "final answer"
    ])

    cancel_flag = False

    def check_cancel():
        return cancel_flag

    def dispatch_fn(tool, params):
        nonlocal cancel_flag
        # Cancel the loop when the first tool is dispatched!
        cancel_flag = True
        return {"nodes": []}

    result = react_loop(
        question="list nodes",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=3,
        is_cancelled=check_cancel,
    )

    assert result.error == "Cancelled"
    assert result.answer == "[Investigation cancelled by user]"
    # It should have run only the first step and aborted before the second LLM generation.
    assert len(result.steps) == 1
    assert result.steps[0].action == "get_nodes"


def test_react_loop_recovery_malformed_json():
    # Return a malformed response first (unquoted action value to fail regex salvage), then a valid answer
    provider = SequencedProvider([
        '{"thought": "broken JSON thought", "action": get_nodes, "params": {}}',
        '{"thought": "recovered now", "action": "answer", "answer": "recovered final answer"}',
        "recovered final answer content"
    ])

    calls = []

    def dispatch_fn(tool, params):
        calls.append((tool, params))
        return {}

    result = react_loop(
        question="list nodes",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=3,
    )

    # Since the first output was broken JSON, the loop should request a retry with the nudge.
    # The provider will then receive the second response which is correct, and complete.
    assert result.answer == "recovered final answer content"
    assert result.error is None
    # The first broken step is not appended, only the answer step
    assert len(result.steps) == 1
    assert result.steps[0].action == "answer"
    assert calls == []


def test_trim_observations_condenses_oldest_first_and_preserves_step_count():
    """Observations must never be dropped — early findings anchor causal
    chains (pod → service → PVC). Trim by condensing oldest bodies while
    the most recent stays intact.
    """
    from react import _trim_observations, MAX_CONTEXT_CHARS, TRIMMED_OBS_CHARS, _TRIM_MARKER

    # Total 15000 > MAX_CONTEXT_CHARS (12000). Oldest gets condensed.
    obs = ["a" * 5000, "b" * 5000, "c" * 5000]
    _trim_observations(obs)

    # No step was dropped — every one still gets a slot.
    assert len(obs) == 3
    # Oldest was condensed to head + marker.
    assert obs[0] == "a" * TRIMMED_OBS_CHARS + _TRIM_MARKER
    # Middle and newest are untouched.
    assert obs[1] == "b" * 5000
    assert obs[2] == "c" * 5000
    # Total is now within the budget.
    assert sum(len(o) for o in obs) <= MAX_CONTEXT_CHARS


def test_trim_observations_no_op_when_under_budget():
    from react import _trim_observations

    obs = ["short a", "short b", "short c"]
    original = list(obs)
    _trim_observations(obs)
    assert obs == original


def test_trim_observations_condenses_multiple_oldest_when_needed():
    """When a single condensation isn't enough, keep working forward."""
    from react import _trim_observations, MAX_CONTEXT_CHARS, TRIMMED_OBS_CHARS, _TRIM_MARKER

    # 4 x 5000 = 20000 > 12000; condense oldest two.
    obs = ["a" * 5000, "b" * 5000, "c" * 5000, "d" * 5000]
    _trim_observations(obs)

    assert len(obs) == 4
    assert obs[0].endswith(_TRIM_MARKER)
    assert obs[1].endswith(_TRIM_MARKER)
    # Most recent is still intact.
    assert obs[-1] == "d" * 5000
    assert sum(len(o) for o in obs) <= MAX_CONTEXT_CHARS


def test_trim_observations_idempotent_on_already_condensed():
    """Running trim twice must not further trim already-condensed entries."""
    from react import _trim_observations

    obs = ["a" * 5000, "b" * 5000, "c" * 5000]
    _trim_observations(obs)
    snapshot = list(obs)
    _trim_observations(obs)
    assert obs == snapshot


def test_trim_observations_last_resort_trims_huge_recent_observation():
    """A single oversized recent observation must also get trimmed — but
    never below TRIMMED_OBS_CHARS so the LLM still sees the head of it."""
    from react import _trim_observations, MAX_CONTEXT_CHARS, TRIMMED_OBS_CHARS, _TRIM_MARKER

    obs = ["a" * 100, "b" * 100, "z" * 20000]
    _trim_observations(obs)

    assert len(obs) == 3
    # Head is preserved with the trim marker at the end.
    assert obs[-1].startswith("z")
    assert obs[-1].endswith(_TRIM_MARKER)
    # Never trimmed below the floor.
    assert len(obs[-1]) >= TRIMMED_OBS_CHARS
    assert sum(len(o) for o in obs) <= MAX_CONTEXT_CHARS + len(_TRIM_MARKER)


def test_react_loop_finalize_cancellation():
    cancel_flag = False
    
    def check_cancel():
        return cancel_flag

    def dispatch_fn(tool, params):
        return {}

    class CancellingProvider:
        def __init__(self):
            self.turns = 0
            
        def generate_stream(self, prompt, system=None, temperature=0.1, max_tokens=8000):
            self.turns += 1
            if self.turns == 1:
                yield '{"thought":"I have enough info","action":"answer","answer":"draft answer"}'
            else:
                nonlocal cancel_flag
                yield "chunk 1"
                cancel_flag = True
                yield "chunk 2"

    provider = CancellingProvider()
    result = react_loop(
        question="list nodes",
        history=[],
        provider=provider,
        dispatch_fn=dispatch_fn,
        max_iterations=3,
        is_cancelled=check_cancel,
    )
    
    # The streaming finalize should abort after "chunk 1" due to cancel_flag setting to True
    assert result.answer == "chunk 1"
