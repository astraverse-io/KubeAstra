"""Unit tests for Kubernetes prompt classification guard."""

from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1]
for path in (BACKEND_DIR,):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from routers import chat
from routers.chat import (
    _augment_context_listing,
    _friendly_summary,
    _looks_like_cluster_context_prompt,
    _looks_like_kubernetes_prompt,
    _looks_like_live_kubernetes_prompt,
    _looks_like_static_kb_lookup,
    _should_protect_from_fast_path,
    _should_run_proactive_triage,
    _should_skip_rag_for_prompt,
    _synthesize_answer,
)


def test_looks_like_kubernetes_prompt_k8s_keywords():
    assert _looks_like_kubernetes_prompt("kubectl get pods") is True
    assert _looks_like_kubernetes_prompt("tell me about kubernetes namespaces") is True
    assert _looks_like_kubernetes_prompt("what is the cpu capacity?") is True


def test_looks_like_kubernetes_prompt_dotted_hostnames():
    # Valid hostname signals
    assert _looks_like_kubernetes_prompt("check node k8s-worker-01.example.com") is True
    assert _looks_like_kubernetes_prompt("is node-1.com reachable?") is True
    assert _looks_like_kubernetes_prompt("ping worker-02.local") is True
    assert _looks_like_kubernetes_prompt("cluster.corp status") is True


def test_looks_like_kubernetes_prompt_programming_false_positives():
    # File extensions ignored
    assert _looks_like_kubernetes_prompt("explain foo.bar.py") is False
    assert _looks_like_kubernetes_prompt("read requirements.txt") is False

    # Python versions / floating point ignored
    assert _looks_like_kubernetes_prompt("is python 3.12 supported?") is False
    assert _looks_like_kubernetes_prompt("set value to 1.0") is False
    assert _looks_like_kubernetes_prompt("does v1.2.3 support this API?") is False
    assert _looks_like_kubernetes_prompt("upgrade to 1.2.3-rc1") is False

    # Package/identifier formats ignored (no digit/hyphen, standard extensions)
    assert _looks_like_kubernetes_prompt("import django.conf.urls") is False
    assert _looks_like_kubernetes_prompt("sys.path.append() implementation") is False
    assert _looks_like_kubernetes_prompt("django.utils.translation behavior") is False


def test_looks_like_live_kubernetes_prompt_distinguishes_static_knowledge():
    assert _looks_like_kubernetes_prompt("what is a Kubernetes pod?") is True
    assert _looks_like_live_kubernetes_prompt("what is a Kubernetes pod?") is False
    assert _looks_like_kubernetes_prompt("what does CrashLoopBackOff mean?") is True
    assert _looks_like_live_kubernetes_prompt("what does CrashLoopBackOff mean?") is False


def test_looks_like_live_kubernetes_prompt_detects_cluster_state():
    assert _looks_like_live_kubernetes_prompt("show allocated resources on node k8s-worker-01") is True
    assert _looks_like_live_kubernetes_prompt("list pods in namespace default") is True
    assert _looks_like_live_kubernetes_prompt("check node k8s-worker-01.example.com") is True


def test_cluster_context_prompts_are_live_and_skip_rag():
    prompt = "What clusters do I have configured?"

    assert _looks_like_cluster_context_prompt(prompt) is True
    assert _looks_like_live_kubernetes_prompt(prompt) is True
    assert _should_skip_rag_for_prompt(prompt) is True
    assert _should_run_proactive_triage(prompt, [], True) is False


def test_static_ansible_kb_lookup_is_protected_from_fast_path():
    prompt = "what is the Ansible playbook that used to deploy rabbitMQ"

    assert _looks_like_kubernetes_prompt(prompt) is False
    assert _looks_like_live_kubernetes_prompt(prompt) is False
    assert _looks_like_static_kb_lookup(prompt) is True
    assert _should_protect_from_fast_path(prompt) is True


def test_static_ansible_repo_concepts_are_kb_lookups():
    prompts = [
        "which role deploys rabbitmq",
        "where is group_vars for rabbitmq",
        "show inventory settings for platform",
        "find the template used by helm_rabbitmq",
    ]

    for prompt in prompts:
        assert _looks_like_static_kb_lookup(prompt), prompt
        assert _should_protect_from_fast_path(prompt), prompt


def test_context_listing_includes_session_selected_cluster(monkeypatch):
    monkeypatch.setattr(chat.db, "get_cluster_connection", lambda session_id: {
        "mode": "autodetect",
        "context_name": "Local Cluster",
        "cluster_name": "dev-cls01",
        "server_url": "https://10.0.0.1",
        "namespace": "default",
        "kubeconfig_path": None,
    })

    result = _augment_context_listing(
        {"success": True, "contexts": [], "current_context": None, "total_contexts": 0},
        session_id="session-a",
    )

    assert result["current_context"] == "Local Cluster"
    assert result["total_contexts"] == 1
    assert result["contexts"][0]["name"] == "Local Cluster"
    assert result["contexts"][0]["source"] == "session_connection"


def _node_result():
    return {
        "name": "k8s-worker-01",
        "query": "k8s-worker-01",
        "status": "Ready",
        "roles": ["worker"],
        "capacity": {"cpu": "16", "cpu_millicores": 16000, "memory_gib": 31.085},
        "allocatable": {"cpu": "16", "cpu_millicores": 16000, "memory_gib": 30.987},
        "allocated": {
            "cpu_requests_millicores": 300,
            "cpu_requests_cores": 0.3,
            "cpu_requests_percent_of_allocatable": 1.88,
            "cpu_limits_millicores": 150,
            "cpu_limits_cores": 0.15,
            "cpu_limits_percent_of_allocatable": 0.94,
            "memory_requests_gib": 0.262,
            "memory_requests_percent_of_allocatable": 0.84,
            "memory_limits_gib": 0.188,
            "memory_limits_percent_of_allocatable": 0.61,
            "non_terminated_pods": 6,
        },
        "pods": [{"namespace": "kube-system", "name": "kube-proxy", "cpu_requests_millicores": 0}],
    }


def test_friendly_summary_answers_node_cpu_allocation_directly():
    reply = _friendly_summary("investigate_node", _node_result(), "node details")

    assert "`k8s-worker-01`" in reply
    assert "0.3 cores requested" in reply
    assert "0.15 cores limited" in reply
    assert "6 non-terminated pods" in reply


def test_synthesize_answer_passes_compact_node_allocation(monkeypatch):
    prompts = []

    class Provider:
        enabled = True

        def generate(self, prompt, system=None, temperature=0.1, max_tokens=800):
            prompts.append((prompt, system, max_tokens))
            return "node cpu answer"

    monkeypatch.setattr(chat, "_llm_provider", lambda: Provider())

    answer, error = _synthesize_answer(
        "cpu allocated to k8s-worker-01",
        "investigate_node",
        _node_result(),
    )

    assert error is None
    assert answer == "node cpu answer"
    prompt, system, max_tokens = prompts[0]
    assert '"allocated"' in prompt
    assert '"cpu_requests_cores": 0.3' in prompt
    assert "labels" not in prompt
    assert "For node CPU/resource allocation questions" in system
    assert max_tokens == 800


def test_synthesize_answer_combines_verified_and_advisory_pod_evidence(monkeypatch):
    class Provider:
        enabled = True

        def generate(self, *args, **kwargs):
            raise AssertionError("verified pod evidence should not need LLM synthesis")

    monkeypatch.setattr(chat, "_llm_provider", lambda: Provider())

    answer, error = _synthesize_answer(
        "why are kafka pods in crashloop",
        "investigate_pod",
        {
            "pod_name": "my-kafka-0",
            "namespace": "infrastructure",
            "evidence_summary": {
                "suspected_root_cause": (
                    "Kafka is configured to use ZooKeeper service `zookeeper-kube-upd-cs`, "
                    "but that service does not exist in namespace `infrastructure`."
                ),
                "suggested_fix": "Restore the missing ZooKeeper service.",
            },
            "container_log_findings": [
                {
                    "container": "prometheus-jmx-exporter",
                    "reason": "CrashLoopBackOff",
                    "restart_count": 6,
                    "logs_previous": {
                        "excerpt": (
                            "Error: Unable to access jarfile "
                            "/opt/jmx_exporter/jmx_prometheus_javaagent.jar"
                        )
                    },
                }
            ],
            "ai": {
                "ai_analysis": {
                    "root_cause": "The Prometheus JMX exporter container cannot find its Java agent jar.",
                    "solution": "Mount the expected JMX exporter jar or fix the container command.",
                }
            },
        },
    )

    assert error is None
    assert "ZooKeeper service `zookeeper-kube-upd-cs`" in answer
    assert "Prometheus JMX exporter" in answer
    assert "prometheus-jmx-exporter" in answer
    assert "Unable to access jarfile" in answer
    assert "Restore the missing ZooKeeper service" in answer
