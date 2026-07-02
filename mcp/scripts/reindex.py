#!/usr/bin/env python3
"""CronJob entrypoint for the RAG ingestion pipeline.

Reads a YAML config (path from ``RAG_CONFIG`` env var or first CLI arg),
spins up the configured sources, and ingests each into its declared
collection (``collection:`` field per source, defaults to ``devops_doc``
for back-compat).

Config shape:

    sources:
      # General team docs into devops_doc (default)
      - kind: local_path
        path: /knowledge
      - kind: git_repo
        url: https://github.com/your-org/devops-runbooks
        branch: main
        token_env: GITHUB_TOKEN
        subdir: docs

      # The deployment repo into deployment_repo. The
      # ``emit_role_aggregates`` flag adds a second pass that yields
      # one aggregate document per role for coarse retrieval (plan §11.3).
      - kind: git_repo
        url: https://github.com/kubeastra/deployment-provisioning.git
        branch: main
        subdir: ansible
        token_env: GITHUB_TOKEN
        emit_role_aggregates: true
        collection: deployment_repo

    chunking:
      max_tokens: 400
      overlap_tokens: 60

For local dev (a clone already on disk), use ``local_path`` for the file
pass and ``role_aggregate`` for the second pass:

    sources:
      - kind: local_path
        path: /Users/me/deployment-provisioning/ansible
        collection: deployment_repo
      - kind: role_aggregate
        path: /Users/me/deployment-provisioning/ansible
        repo_url: https://github.com/kubeastra/deployment-provisioning.git
        branch: main
        path_prefix: ansible
        collection: deployment_repo

Exit codes: 0 on success (even with per-doc failures, since failures are
visible in logs and stats); 1 only on hard failures (bad config, can't
connect to Qdrant).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

# Make the project importable when run directly from scripts/.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from services.rag.ansible_roles import RoleAggregateSource
from services.rag.ingestion import IngestStats, ingest
from services.rag.schema import DEVOPS_DOC, get_collection
from services.rag.sources.git_repo import GitRepoSource
from services.rag.sources.local_path import LocalPathSource
from services.vector_db import vector_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("rag.reindex")


_SOURCE_CTORS = {
    "local_path": LocalPathSource,
    "git_repo": GitRepoSource,
    "role_aggregate": RoleAggregateSource,
}

# Keys consumed by reindex itself rather than passed to the source ctor.
_ORCHESTRATOR_KEYS = {"kind", "collection"}


def _load_config(path: str) -> dict:
    try:
        import yaml  # PyYAML is already a transitive dep via pydantic et al.
    except ImportError as exc:
        logger.error("PyYAML is required for reindex.py. Install with: pip install pyyaml")
        raise SystemExit(1) from exc
    try:
        with open(path, "r") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("RAG config not found at %s", path)
        raise SystemExit(1)
    except Exception as exc:
        logger.error("Failed to parse RAG config %s: %s", path, exc)
        raise SystemExit(1)


def _build_jobs(specs: list[dict]):
    """Return a list of ``(source, target_spec)`` tuples ready for ``ingest()``.

    Each spec drops ``kind`` and ``collection`` before being splatted into
    the source constructor, so unknown source-specific kwargs propagate
    naturally.
    """
    jobs = []
    for spec in specs:
        kind = (spec or {}).get("kind", "").strip()
        ctor = _SOURCE_CTORS.get(kind)
        if ctor is None:
            logger.warning("Unknown source kind %r — skipping", kind)
            continue
        target_name = (spec or {}).get("collection") or DEVOPS_DOC.name
        target_spec = get_collection(target_name)
        if target_spec is None:
            logger.warning(
                "Unknown target collection %r for source %s — skipping",
                target_name, kind,
            )
            continue
        kwargs = {k: v for k, v in spec.items() if k not in _ORCHESTRATOR_KEYS}
        try:
            source = ctor(**kwargs)
        except Exception as exc:
            logger.warning("Failed to build source %r: %s", spec, exc)
            continue
        jobs.append((source, target_spec))
    return jobs


def main(argv: list[str]) -> int:
    config_path = os.environ.get("RAG_CONFIG") or (argv[1] if len(argv) > 1 else "")
    if not config_path:
        logger.error("Provide RAG_CONFIG env var or path argument.")
        return 1

    cfg = _load_config(config_path)
    jobs = _build_jobs(cfg.get("sources") or [])
    if not jobs:
        logger.error("No usable sources in config — nothing to ingest.")
        return 1

    chunking = cfg.get("chunking") or {}
    max_tokens = int(chunking.get("max_tokens", 400))
    overlap_tokens = int(chunking.get("overlap_tokens", 60))

    try:
        vector_db.connect()
    except Exception as exc:
        logger.error("Cannot connect to vector DB: %s", exc)
        return 1

    overall = IngestStats()
    try:
        for source, target in jobs:
            logger.info(
                "Ingesting source kind=%s into collection=%s",
                getattr(source, "name", "?"), target.name,
            )
            stats = ingest(
                source,
                target=target,
                max_tokens=max_tokens,
                overlap_tokens=overlap_tokens,
            )
            overall.merge(stats)
    finally:
        vector_db.disconnect()

    # Stamp the wall-clock finish so duration_seconds in INGEST_SUMMARY
    # reflects the full multi-job run, not just one source.
    overall.finished_at = time.time()
    # Emit a machine-readable summary so cluster log aggregators can alert
    # on it without parsing prose.
    print("INGEST_SUMMARY " + json.dumps(overall.to_dict()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
