#!/usr/bin/env python3
"""Local smoke test for the deployment-repo RAG pipeline.

Ingests the local clone at ``deployment-provisioning/ansible`` into an
in-memory Qdrant, then runs a handful of representative Ansible-error
queries against it. Prints top-K hits with citations so a human can
eyeball whether retrieval looks sane.

Usage:
    venv/bin/python3 scripts/smoke_deployment_repo.py
    # or with a custom repo root:
    venv/bin/python3 scripts/smoke_deployment_repo.py /path/to/ansible

No external services required (in-memory Qdrant, local MiniLM embedding).
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from qdrant_client import QdrantClient  # noqa: E402

from services.embeddings import embeddings  # noqa: E402
from services.rag.ansible_roles import RoleAggregateSource  # noqa: E402
from services.rag.ingestion import ingest  # noqa: E402
from services.rag.schema import DEPLOYMENT_REPO  # noqa: E402
from services.rag.sources.local_path import LocalPathSource  # noqa: E402
from services import vector_db as vdb_module  # noqa: E402


DEFAULT_REPO = (
    "/Users/pruthvidavineni/AI_DevOps_Assistant/kubeastra-ai-assistant/"
    "deployment-provisioning/ansible"
)

# Canned Ansible-flavored error pastes. Each is a free-text query whose
# top hits should plausibly include a related role/playbook/module from
# the indexed repo. Tuned to exercise different chunkers + metadata.
SMOKE_QUERIES = [
    "TASK [kubernetes/kube_check_health : Check kubernetes Nodes] "
    "fatal: kubernetes.core.k8s_info: Failed to import the required Python library "
    "(kubernetes) on the host",
    "PLAY RECAP failed: deploy AWX execution environment, "
    "create AWX credentials, awx api connection refused",
    "Error: rabbitmq queue purge failed, no module named aiohttp on remote",
    "elastic-secret rendered with empty central_elastic_password, "
    "metricbeat pod cannot decode base64",
    "KubeAstra install: msui_deploy role failing at template rendering for nginx",
]


def main() -> int:
    repo = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REPO
    repo_path = Path(repo)
    if not repo_path.is_dir():
        print(f"ERROR: {repo} is not a directory", file=sys.stderr)
        return 1

    # ── Wire vector_db to an in-memory Qdrant ────────────────────────────
    client = QdrantClient(":memory:")
    vdb_module.vector_db._client = client
    vdb_module.vector_db.ensure_collection_for(DEPLOYMENT_REPO)

    # ── Ingest both passes into deployment_repo collection ───────────────
    print(f"== Ingesting files from {repo}")
    files_src = LocalPathSource(repo)
    s1 = ingest(files_src, target=DEPLOYMENT_REPO)
    print(f"   files: {s1.to_dict()}")

    print("== Ingesting per-role aggregates")
    agg_src = RoleAggregateSource(
        repo,
        repo_url="https://github.com/kubeastra/deployment-provisioning.git",
        branch="main",
        path_prefix="ansible",
    )
    s2 = ingest(agg_src, target=DEPLOYMENT_REPO)
    print(f"   aggs:  {s2.to_dict()}")
    print()

    # ── Query and pretty-print top-K ─────────────────────────────────────
    for i, q in enumerate(SMOKE_QUERIES, start=1):
        print(f"━━ Q{i}: {q!r}")
        qvec = embeddings.embed(q)
        hits = vdb_module.vector_db.search_in(
            collection=DEPLOYMENT_REPO.name,
            query_vector=qvec,
            limit=5,
        )
        if not hits:
            print("   (no hits)")
            continue
        for rank, h in enumerate(hits, start=1):
            url = h.get("url", "")
            sec = h.get("section", "")
            kind = h.get("kind", "")
            role = h.get("role", "")
            module = h.get("module", "")
            sim = h.get("similarity")
            print(
                f"   #{rank}  sim={sim}  kind={kind}  role={role}  "
                f"module={module}\n"
                f"        section: {sec}\n"
                f"        url:     {url}"
            )
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
