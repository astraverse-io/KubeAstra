"""RAG router workflow smoke tests.

These exercise route-level decisions with fake embeddings/vector DB objects.
They do not require a live Qdrant instance or a real sentence-transformer.
"""

from pathlib import Path
import sys

MCP_DIR = Path(__file__).resolve().parents[2]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from services.rag import router  # noqa: E402
from services.rag.router import build_grounded_preamble, route  # noqa: E402


class Settings:
    rag_router_enabled = True
    rag_router_collections = "runbook,devops_doc"
    rag_router_top_k = 3
    rag_router_cached_threshold = 0.97
    rag_router_grounded_threshold = 0.82
    enable_audit_log = False
    audit_log_path = "/tmp/unused-audit.log"


class FakeEmbeddings:
    def embed(self, text):
        return [0.0] * 384


class FakeVectorDB:
    def __init__(self, hits_by_collection):
        self.hits_by_collection = hits_by_collection
        self.searched = []

    def connect(self):
        return None

    def search_in(self, collection, query_vector, filters=None, limit=3, log_failures=True):
        self.searched.append(collection)
        return self.hits_by_collection.get(collection, [])[:limit]


def _patch_router(monkeypatch, hits_by_collection, settings=None):
    from services import embeddings as embeddings_module
    from services import vector_db as vector_db_module

    fake_vdb = FakeVectorDB(hits_by_collection)
    monkeypatch.setattr(router, "get_settings", lambda: settings or Settings())
    monkeypatch.setattr(embeddings_module, "embeddings", FakeEmbeddings())
    monkeypatch.setattr(vector_db_module, "vector_db", fake_vdb)
    return fake_vdb


def test_rag_router_returns_cached_verified_runbook(monkeypatch):
    fake_vdb = _patch_router(
        monkeypatch,
        {
            "runbook": [{
                "similarity": 0.99,
                "verified": True,
                "title": "Kafka CrashLoop Runbook",
                "problem": "Kafka cannot reach ZooKeeper",
                "resolution": "Restore ZooKeeper service or update KAFKA_ZOOKEEPER_CONNECT.",
                "commands": "kubectl get svc -n infrastructure",
                "url": "https://docs.example/runbooks/kafka",
            }],
            "devops_doc": [],
        },
    )

    decision = route("kafka pods crashloop because zookeeper is missing")

    assert decision.mode == "cached"
    assert decision.top_collection == "runbook"
    assert decision.cached_answer
    assert "Kafka CrashLoop Runbook" in decision.cached_answer
    assert "kubectl get svc" in decision.cached_answer
    assert fake_vdb.searched == ["runbook", "devops_doc"]


def test_rag_router_returns_grounded_doc_and_preamble(monkeypatch):
    _patch_router(
        monkeypatch,
        {
            "runbook": [],
            "devops_doc": [{
                "similarity": 0.90,
                "title": "Kafka Operations",
                "section": "ZooKeeper checks",
                "content": "Check the ZooKeeper service and endpoints before restarting Kafka.",
                "url": "https://docs.example/kafka",
            }],
        },
    )

    decision = route("how should I validate kafka zookeeper dependencies?")
    preamble = build_grounded_preamble(decision)

    assert decision.mode == "grounded"
    assert decision.top_collection == "devops_doc"
    assert decision.citations[0].title == "Kafka Operations"
    assert "Check the ZooKeeper service" in preamble


def test_rag_router_force_includes_deployment_repo_for_ansible_errors(monkeypatch):
    class AnsibleSettings(Settings):
        rag_router_collections = "runbook"

    fake_vdb = _patch_router(
        monkeypatch,
        {
            "runbook": [],
            "deployment_repo": [{
                "similarity": 0.88,
                "title": "kube_check_health role",
                "section": "tasks/main.yaml",
                "content": "The role uses kubernetes.core.k8s_info to inspect nodes.",
                "url": "https://github.com/example/deployment/blob/main/roles/kubernetes/kube_check_health/tasks/main.yaml",
            }],
        },
        settings=AnsibleSettings(),
    )

    decision = route("TASK [kubernetes/kube_check_health : Check kubernetes Nodes] *** fatal:")

    assert fake_vdb.searched == ["runbook", "deployment_repo"]
    assert decision.mode == "grounded"
    assert decision.ansible_detected is True
    assert decision.top_collection == "deployment_repo"


def test_rag_router_force_includes_deployment_repo_for_ansible_lookup(monkeypatch):
    class AnsibleSettings(Settings):
        rag_router_collections = "runbook"

    fake_vdb = _patch_router(
        monkeypatch,
        {
            "runbook": [],
            "deployment_repo": [{
                "similarity": 0.88,
                "title": "playbooks/ops/deploy_rabbit.yaml",
                "section": "ops > Deploy RabbitMQ Standard (default)",
                "content": "- name: Deploy RabbitMQ Standard\n  import_playbook: deploy_rabbit_old.yaml",
                "url": "https://github.com/example/deployment/blob/main/ansible/playbooks/ops/deploy_rabbit.yaml",
            }],
        },
        settings=AnsibleSettings(),
    )

    decision = route("what is the Ansible playbook that used to deploy RabbitMQ")

    assert fake_vdb.searched == ["runbook", "deployment_repo"]
    assert decision.mode == "grounded"
    assert decision.ansible_detected is True
    assert decision.top_collection == "deployment_repo"


def test_rag_router_ansible_lookup_falls_back_cold_when_repo_hit_is_weak(monkeypatch):
    class AnsibleSettings(Settings):
        rag_router_collections = "runbook"

    fake_vdb = _patch_router(
        monkeypatch,
        {
            "runbook": [],
            "deployment_repo": [{
                "similarity": 0.40,
                "title": "roles/unrelated/tasks/main.yml",
                "content": "Unrelated role content",
            }],
        },
        settings=AnsibleSettings(),
    )

    decision = route("which role deploys rabbitmq")

    assert fake_vdb.searched == ["runbook", "deployment_repo"]
    assert decision.ansible_detected is True
    assert decision.top_collection == "deployment_repo"
    assert decision.mode == "cold"
    assert "below grounded threshold" in decision.reason


def test_rag_router_falls_back_cold_below_threshold(monkeypatch):
    _patch_router(
        monkeypatch,
        {
            "runbook": [{"similarity": 0.40, "title": "Weak hit", "content": "not relevant"}],
            "devops_doc": [],
        },
    )

    decision = route("unrelated live cluster state question")

    assert decision.mode == "cold"
    assert decision.top_score == 0.40
    assert "below grounded threshold" in decision.reason
