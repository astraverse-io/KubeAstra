"""Backend workflow tests for prompt -> tool -> response behavior.

These sit above helper-level routing tests and below the FastAPI endpoint:
they exercise the single-shot chat path with mocked dispatch/LLM boundaries.
"""

from pathlib import Path
import sys
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from routers import chat  # noqa: E402
from routers.chat import ChatRequest, _chat_react, _chat_single_shot, _compact_params, _prompt_preview  # noqa: E402


def _persisted():
    calls = []

    def persist(role, content, **kwargs):
        calls.append({"role": role, "content": content, **kwargs})

    return calls, persist


def test_single_shot_crashloop_prompt_dispatches_filtered_pod_inventory(monkeypatch):
    dispatched = []

    def fake_dispatch(tool, params, **kwargs):
        dispatched.append((tool, params))
        return {
            "namespace": "*",
            "status_filter": "CrashLoopBackOff",
            "pod_count": 1,
            "pods": [
                {"namespace": "infrastructure", "name": "my-kafka-0", "status": "CrashLoopBackOff", "ready": "0/2", "restarts": 42}
            ],
        }

    monkeypatch.setattr(chat, "_dispatch", fake_dispatch)
    monkeypatch.setattr(chat, "_llm_provider", lambda model=None: None)
    persisted, persist = _persisted()

    response = _chat_single_shot(
        ChatRequest(message="any pods in crashloop status"),
        persist,
        "test-session",
    )

    assert dispatched == [("get_pods", {"namespace": "*", "status_filter": "CrashLoopBackOff"})]
    assert response.tool_used == "get_pods"
    assert response.result["status_filter"] == "CrashLoopBackOff"
    assert response.result["pods"][0]["name"] == "my-kafka-0"
    assert persisted[-1]["tool_used"] == "get_pods"


def test_single_shot_node_cpu_prompt_returns_concise_allocation_summary(monkeypatch):
    def fake_dispatch(tool, params, **kwargs):
        assert (tool, params) == ("investigate_node", {"node_name": "node-a"})
        return {
            "name": "node-a",
            "allocatable": {"cpu": "16", "cpu_millicores": 16000, "memory_gib": 30.987},
            "allocated": {
                "cpu_requests_cores": 0.3,
                "cpu_requests_percent_of_allocatable": 1.88,
                "cpu_limits_cores": 0.15,
                "cpu_limits_percent_of_allocatable": 0.94,
                "non_terminated_pods": 6,
            },
            "labels": {"flannel.alpha.coreos.com/backend-data": "noisy"},
        }

    monkeypatch.setattr(chat, "_dispatch", fake_dispatch)
    monkeypatch.setattr(chat, "_llm_provider", lambda model=None: None)
    _, persist = _persisted()

    response = _chat_single_shot(ChatRequest(message="cpu allocated to node-a"), persist, "test-session")

    assert response.tool_used == "investigate_node"
    assert "0.3 cores requested" in response.reply
    assert "0.15 cores limited" in response.reply
    assert "flannel.alpha" not in response.reply


def test_single_shot_node_labels_prompt_keeps_focused_result_shape(monkeypatch):
    def fake_dispatch(tool, params, **kwargs):
        assert (tool, params) == ("get_nodes", {"labels_only": True})
        return {
            "node_count": 1,
            "labels_only": True,
            "nodes": [{"name": "node-a", "labels": {"zone": "east"}, "label_count": 1}],
        }

    monkeypatch.setattr(chat, "_dispatch", fake_dispatch)
    monkeypatch.setattr(chat, "_llm_provider", lambda model=None: None)
    _, persist = _persisted()

    response = _chat_single_shot(ChatRequest(message="get all node labels for all nodes"), persist, "test-session")

    assert response.tool_used == "get_nodes"
    assert response.result == {
        "node_count": 1,
        "labels_only": True,
        "nodes": [{"name": "node-a", "labels": {"zone": "east"}, "label_count": 1}],
    }


def test_structured_chat_logging_compacts_sensitive_params():
    compact = _compact_params({
        "namespace": "apps",
        "token": "secret-token",
        "password": "secret-password",
        "labels": {"app": "web"},
        "items": [1, 2, 3],
    })

    assert compact["namespace"] == "apps"
    assert compact["token"] == "<redacted>"
    assert compact["password"] == "<redacted>"
    assert compact["labels"] == "dict[1]"
    assert compact["items"] == "list[3]"


def test_structured_chat_logging_redacts_prompt_previews():
    preview = _prompt_preview("Authorization: Bearer secret-token-value")

    assert "secret-token-value" not in preview
    assert "<REDACTED:bearer_header>" in preview


def test_react_static_ansible_kb_lookup_returns_grounded_playbook(monkeypatch):
    class Provider:
        enabled = True

        def generate_stream(self, *args, **kwargs):
            raise AssertionError("static grounded KB lookup should bypass ReAct LLM")

    class Decision:
        mode = "grounded"
        top_score = 0.789
        top_collection = "deployment_repo"
        ansible_detected = True
        grounded_chunks = [
            {
                "title": "playbooks/ops/deploy_rabbit.yaml",
                "section": "ops > Deploy RabbitMQ Standard (default)",
                "content": "- name: Deploy RabbitMQ Standard (default)\n  import_playbook: deploy_rabbit_old.yaml\n",
                "url": "https://github.com/example/deployment/blob/develop/ansible/playbooks/ops/deploy_rabbit.yaml",
                "similarity": 0.789,
            },
            {
                "title": "playbooks/ops/deploy_rabbit.yaml",
                "section": "ops > Deploy RabbitMQ Instance",
                "content": "- name: Deploy RabbitMQ Instance\n  import_playbook: deploy_rabbitmq_instance.yaml\n",
                "url": "https://github.com/example/deployment/blob/develop/ansible/playbooks/ops/deploy_rabbit.yaml",
                "similarity": 0.750,
            },
        ]

        def to_dict(self):
            return {
                "mode": self.mode,
                "top_score": self.top_score,
                "top_collection": self.top_collection,
                "ansible_detected": self.ansible_detected,
                "grounded_chunks": self.grounded_chunks,
            }

    from services.rag import router as rag_router

    monkeypatch.setattr(rag_router, "route", lambda question: Decision())
    monkeypatch.setattr(rag_router, "build_grounded_preamble", lambda decision: "should not be used")
    monkeypatch.setattr(chat.memory, "build_memory_preamble", lambda session_id: "")
    monkeypatch.setattr(chat, "_maybe_capture_chat", lambda **kwargs: None)

    persisted, persist = _persisted()

    response = _chat_react(
        ChatRequest(message="what is the Ansible playbook that used to deploy rabbitMQ"),
        Provider(),
        persist,
        "test-session",
    )

    assert response.tool_used == "rag_grounded"
    assert response.result["rag_decision"]["top_collection"] == "deployment_repo"
    assert "`playbooks/ops/deploy_rabbit.yaml`" in response.reply
    assert "`deploy_rabbit_old.yaml`" in response.reply
    assert "`deploy_rabbitmq_instance.yaml`" in response.reply
    assert "Kubernetes clusters do not store" not in response.reply
    assert persisted[-1]["tool_used"] == "rag_grounded"


def test_react_ansible_lookup_falls_back_to_llm_when_rag_is_cold(monkeypatch):
    class Provider:
        enabled = True

    class ColdDecision:
        mode = "cold"
        top_score = 0.40
        top_collection = "deployment_repo"
        grounded_chunks = []
        ansible_detected = True
        reason = "top similarity 0.400 below grounded threshold 0.600"

        def to_dict(self):
            return {
                "mode": self.mode,
                "top_score": self.top_score,
                "top_collection": self.top_collection,
                "ansible_detected": self.ansible_detected,
                "reason": self.reason,
                "grounded_chunks": [],
            }

    calls = []

    def fake_react_loop(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            answer="LLM fallback answer",
            tool_used="none",
            result={"source": "llm"},
            error=None,
            steps=[],
            total_iterations=1,
            total_duration_ms=1.0,
            suggested_actions=[],
            synthesis_breakdown={"diagnosis": "LLM fallback answer"},
        )

    from services.rag import router as rag_router
    import react

    monkeypatch.setattr(rag_router, "route", lambda question: ColdDecision())
    monkeypatch.setattr(rag_router, "build_grounded_preamble", lambda decision: "should not be used")
    monkeypatch.setattr(react, "react_loop", fake_react_loop)
    monkeypatch.setattr(react, "build_envelope_retrieval_context", lambda steps: [])
    monkeypatch.setattr(chat.memory, "build_memory_preamble", lambda session_id: "")
    monkeypatch.setattr(chat, "_maybe_capture_chat", lambda **kwargs: None)

    persisted, persist = _persisted()

    response = _chat_react(
        ChatRequest(message="which role deploys rabbitmq"),
        Provider(),
        persist,
        "test-session",
    )

    assert response.tool_used == "none"
    assert response.reply == "LLM fallback answer"
    assert calls
    assert calls[0]["grounded_preamble"] == ""
    assert persisted[-1]["tool_used"] == "none"
