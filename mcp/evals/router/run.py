#!/usr/bin/env python3
"""Eval the RAG router against a hand-curated golden set (Appendix E §8).

Usage:
    cd mcp
    EVAL_LIVE_LLM=false venv/bin/python -m evals.router.run

The router's `route()` function never calls an LLM directly — it embeds
the query (using sentence-transformers locally) and searches Qdrant
(in-memory mode for tests). So the eval is offline by default and
deterministic enough to run on every commit.

Pass criterion: precision ≥ 0.8 (when router says grounded, the citation
is actually the seeded relevant doc).
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_MCP_DIR = _SCRIPT_DIR.parent.parent
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))


# ── Fake embedder (deterministic, matches test_router.py) ────────────────────

def _fake_embed(text: str) -> list[float]:
    """8-dim hash-derived vector. Identical input → identical vector.

    Crucially, this is the SAME function we use in test_router.py so the
    "similarity → mode" thresholds calibrate against the same noise floor.
    """
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [b / 255.0 for b in h[:8]]


# ── Setup: configure router + Qdrant for the eval run ───────────────────────

def _setup_router_env():
    """Wire env vars + monkey-patch the embedder for deterministic runs."""
    os.environ["RAG_ROUTER_ENABLED"] = "true"
    os.environ["RAG_ROUTER_CACHED_THRESHOLD"] = "0.92"
    os.environ["RAG_ROUTER_GROUNDED_THRESHOLD"] = "0.70"
    os.environ["RAG_ROUTER_TOP_K"] = "5"
    os.environ["RAG_ROUTER_COLLECTIONS"] = "runbook,devops_doc,session_memory"
    os.environ["QDRANT_URL"] = ":memory:"
    os.environ["EMBEDDING_DIM"] = "8"

    from config.settings import get_settings
    get_settings.cache_clear()

    # Replace the embeddings singleton with our fake.
    import services.embeddings as emb_module
    emb_module.embeddings.embed = _fake_embed
    emb_module.embeddings.embed_many = lambda texts: [_fake_embed(t) for t in texts]


def _seed_qdrant(seed_specs: list[dict]) -> None:
    """Upsert seed points into Qdrant before each case runs."""
    from services.vector_db import vector_db, CollectionSpec, COMMON_INDEXED

    # Reset state — every case gets a fresh DB.
    vector_db.disconnect()
    from config.settings import get_settings
    vector_db._settings = get_settings()
    vector_db.connect()

    for spec in seed_specs:
        coll = spec["collection"]
        vector_db.ensure_collection_for(CollectionSpec(
            name=coll, indexed_fields=COMMON_INDEXED,
        ))
        vector = _fake_embed(spec["query"])
        point_id = str(uuid.uuid4())
        vector_db.upsert_point(coll, point_id, spec["payload"], vector)


# ── fn_under_test + scorer ───────────────────────────────────────────────────

def _route_case(case: dict):
    """Seed Qdrant for this case, then call route()."""
    from services.rag import route

    _seed_qdrant(case.get("seed") or [])
    decision = route(case["query"])
    return decision


def _score(case: dict, decision) -> float:
    """1.0 = all assertions met, 0.0 = any assertion failed."""
    expected_mode = case.get("expected_mode")
    if expected_mode and decision.mode != expected_mode:
        return 0.0

    expected_top_collection = case.get("expected_top_collection")
    if expected_top_collection and decision.top_collection != expected_top_collection:
        return 0.0

    expected_min_score = case.get("expected_min_score")
    if expected_min_score is not None and decision.top_score < float(expected_min_score):
        return 0.0

    expected_max_score = case.get("expected_max_score")
    if expected_max_score is not None and decision.top_score > float(expected_max_score):
        return 0.0

    expected_substr = case.get("expected_citation_substr")
    if expected_substr:
        if not decision.citations:
            return 0.0
        if expected_substr not in decision.citations[0].url:
            return 0.0

    return 1.0


# ── Entry point ──────────────────────────────────────────────────────────────

def _setup_router_env_live():
    """LIVE mode — use the real sentence-transformers embedder.

    Same env config as offline but DOES NOT monkey-patch embed(). The
    first call downloads ~90MB of model weights (~5-15s); cached after.
    Requires `EVAL_LIVE_LLM=true` in the env.
    """
    os.environ["RAG_ROUTER_ENABLED"] = "true"
    os.environ["RAG_ROUTER_CACHED_THRESHOLD"] = "0.92"
    os.environ["RAG_ROUTER_GROUNDED_THRESHOLD"] = "0.70"
    os.environ["RAG_ROUTER_TOP_K"] = "5"
    os.environ["RAG_ROUTER_COLLECTIONS"] = "runbook,devops_doc,session_memory"
    os.environ["QDRANT_URL"] = ":memory:"
    # Real model dim — overrides the fake's 8-dim setting.
    os.environ["EMBEDDING_DIM"] = "384"

    from config.settings import get_settings
    get_settings.cache_clear()


def _seed_qdrant_live(seed_specs: list[dict]) -> None:
    """LIVE seed — uses the real embeddings module (no fake)."""
    from services.vector_db import vector_db, CollectionSpec, COMMON_INDEXED
    from services.embeddings import embeddings as _emb

    vector_db.disconnect()
    from config.settings import get_settings
    vector_db._settings = get_settings()
    vector_db.connect()

    for spec in seed_specs:
        coll = spec["collection"]
        vector_db.ensure_collection_for(CollectionSpec(
            name=coll, indexed_fields=COMMON_INDEXED,
        ))
        vector = _emb.embed(spec["query"])
        point_id = str(uuid.uuid4())
        vector_db.upsert_point(coll, point_id, spec["payload"], vector)


def _route_case_live(case: dict):
    from services.rag import route
    _seed_qdrant_live(case.get("seed") or [])
    return route(case["query"])


def main() -> int:
    from evals._runner import run_eval, is_live

    if is_live():
        _setup_router_env_live()
        test_file = str(_SCRIPT_DIR / "golden_set_live.yaml")
        return run_eval(
            test_file=test_file,
            fn_under_test=_route_case_live,
            scorer=_score,
            pass_threshold=1.0,
            description="RAG router (LIVE)",
        )

    _setup_router_env()
    test_file = str(_SCRIPT_DIR / "golden_set.yaml")
    return run_eval(
        test_file=test_file,
        fn_under_test=_route_case,
        scorer=_score,
        pass_threshold=1.0,
        description="RAG router (offline)",
    )


if __name__ == "__main__":
    sys.exit(main())
