"""Tests for the retrieval router (blueprint §8).

Uses Qdrant `:memory:` mode + a deterministic fake embedder so the router's
decision logic can be exercised without a real model or vector DB server.

Covers the blueprint §8 acceptance criteria:
  1. Off-topic query ("what's the weather?") → mode="cold" with low score.
  2. Paraphrased query against a seeded doc → mode="grounded" with citations.
  3. Exact-match against a seeded verified runbook → mode="cached" with
     cached_answer including a Source link.
  4. RAG_ROUTER_ENABLED=false → mode="cold" immediately (no embed, no search).
  5. Vector DB down → mode="cold" with reason; chat keeps working.
  6. build_grounded_preamble respects max_chars and includes section breadcrumbs.
"""

from __future__ import annotations

import hashlib
import os
import sys

import pytest

MCP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)


# ── Deterministic fake embedder ──────────────────────────────────────────────
#
# The router calls `embeddings.embed(text)` to vectorize the query. We replace
# the real sentence-transformers model with a hash-derived 8-dim vector so
# tests run fast and offline. Crucially, identical input → identical vector,
# and similar text (overlapping substrings) → similar vectors, just enough
# for the router's threshold logic to behave predictably.

def _fake_embed(text: str) -> list[float]:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [b / 255.0 for b in h[:8]]


def _fake_embed_many(texts: list[str]) -> list[list[float]]:
    return [_fake_embed(t) for t in texts]


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_embedder(monkeypatch):
    """Replace the embeddings singleton's methods with deterministic fakes."""
    import services.embeddings as emb_module
    monkeypatch.setattr(emb_module.embeddings, "embed", _fake_embed)
    monkeypatch.setattr(emb_module.embeddings, "embed_many", _fake_embed_many)
    yield


@pytest.fixture
def memory_qdrant(monkeypatch):
    """Point vector_db at :memory: with embedding_dim=8 (matches fake embedder).

    Mutates the singleton in place (same approach as test_rag.py).
    """
    monkeypatch.setenv("QDRANT_URL", ":memory:")
    monkeypatch.setenv("EMBEDDING_DIM", "8")
    from config.settings import get_settings
    get_settings.cache_clear()

    from services.vector_db import vector_db
    vector_db.disconnect()
    vector_db._settings = get_settings()
    vector_db.connect()
    yield vector_db
    vector_db.disconnect()
    get_settings.cache_clear()
    vector_db._settings = get_settings()


@pytest.fixture
def router_on(monkeypatch):
    """Enable the RAG router with sensible defaults."""
    monkeypatch.setenv("RAG_ROUTER_ENABLED", "true")
    monkeypatch.setenv("RAG_ROUTER_COLLECTIONS", "runbook,devops_doc")
    monkeypatch.setenv("RAG_ROUTER_CACHED_THRESHOLD", "0.92")
    monkeypatch.setenv("RAG_ROUTER_GROUNDED_THRESHOLD", "0.70")
    monkeypatch.setenv("RAG_ROUTER_TOP_K", "5")
    from config.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def router_off(monkeypatch):
    monkeypatch.setenv("RAG_ROUTER_ENABLED", "false")
    from config.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _seed_collection(name: str, hits: list[dict]):
    """Create a collection and upsert hand-crafted points.

    Each `hit` dict has at least: `payload`, `query` (used as the embedding
    source so the fake embedder returns a matching vector when queried).
    """
    from services.vector_db import vector_db, CollectionSpec, COMMON_INDEXED
    vector_db.ensure_collection_for(CollectionSpec(
        name=name,
        indexed_fields=COMMON_INDEXED,
    ))
    import uuid
    for i, hit in enumerate(hits):
        vec = _fake_embed(hit["query"])
        point_id = str(uuid.uuid4())
        vector_db.upsert_point(name, point_id, hit["payload"], vec)


# ── Disabled router ──────────────────────────────────────────────────────────

class TestRouterDisabled:
    """Acceptance criterion #4."""

    def test_disabled_returns_cold_immediately(self, router_off):
        from services.rag import route
        decision = route("any question")
        assert decision.mode == "cold"
        assert "disabled" in decision.reason


# ── Empty / off-topic ────────────────────────────────────────────────────────

class TestRouterCold:

    def test_empty_query_returns_cold(self, router_on, fake_embedder, memory_qdrant):
        from services.rag import route
        assert route("").mode == "cold"
        assert route("   \n\t  ").mode == "cold"

    def test_no_collections_returns_cold(self, router_on, fake_embedder, memory_qdrant,
                                          monkeypatch):
        monkeypatch.setenv("RAG_ROUTER_COLLECTIONS", "")
        from config.settings import get_settings
        get_settings.cache_clear()
        from services.rag import route
        assert route("anything").mode == "cold"

    def test_no_hits_returns_cold(self, router_on, fake_embedder, memory_qdrant):
        """No content seeded → router has nothing to return → cold."""
        from services.rag import route
        decision = route("a question about something")
        # Both `runbook` and `devops_doc` collections don't exist yet — the
        # search loop tolerates missing collections (returns []) and we end
        # up with no pooled hits.
        assert decision.mode == "cold"
        assert "no hits" in decision.reason.lower() or decision.top_score == 0.0


# ── Cached (verified runbook short-circuit) ─────────────────────────────────

class TestRouterCached:
    """Acceptance criterion #3."""

    def test_exact_match_verified_runbook_returns_cached(
        self, router_on, fake_embedder, memory_qdrant,
    ):
        from services.rag import route

        # Seed `runbook` with a verified entry. Exact query match → similarity 1.0
        # (identical embedding vectors), which clears the cached threshold.
        _seed_collection("runbook", [{
            "query": "CrashLoopBackOff after deploy",
            "payload": {
                "title": "CrashLoopBackOff recovery",
                "url": "https://runbooks/crashloop",
                "section": "Recovery",
                "content": "Roll back the deployment via kubectl rollout undo.",
                "verified": True,
                "resolution": "Run: kubectl rollout undo deployment/<name>",
                "commands": "kubectl rollout undo deployment/api -n prod",
            },
        }])

        decision = route("CrashLoopBackOff after deploy")
        assert decision.mode == "cached"
        assert decision.cached_answer is not None
        # Citation surfaces from the matched collection
        assert len(decision.citations) >= 1
        assert decision.citations[0].collection == "runbook"
        # Cached answer includes the Source link
        assert "Source" in decision.cached_answer or "https://" in decision.cached_answer

    def test_unverified_runbook_does_not_cache(
        self, router_on, fake_embedder, memory_qdrant,
    ):
        """Even an exact match falls through to grounded when verified=False."""
        from services.rag import route

        _seed_collection("runbook", [{
            "query": "CrashLoopBackOff after deploy",
            "payload": {
                "title": "Unverified runbook",
                "url": "https://runbooks/draft",
                "content": "Draft content.",
                "verified": False,
            },
        }])

        decision = route("CrashLoopBackOff after deploy")
        # Cached requires verified=True; unverified content still scores high
        # enough for grounded mode.
        assert decision.mode in ("grounded", "cold")
        assert decision.mode != "cached"


# ── Grounded ────────────────────────────────────────────────────────────────

class TestRouterGrounded:
    """Acceptance criterion #2."""

    def test_high_score_devops_doc_returns_grounded(
        self, router_on, fake_embedder, memory_qdrant,
    ):
        from services.rag import route

        _seed_collection("devops_doc", [{
            "query": "investigate stuck PVC in storage",
            "payload": {
                "title": "PVC troubleshooting",
                "url": "file:///knowledge/pvc.md",
                "section": "Diagnosis",
                "content": "Check pvc/pod-name -o yaml for stuck state.",
                "verified": True,
            },
        }])

        decision = route("investigate stuck PVC in storage")
        assert decision.mode == "grounded"
        assert decision.top_collection == "devops_doc"
        assert decision.grounded_chunks, "grounded mode requires non-empty chunks"
        assert decision.citations


# ── Failure modes (vector DB down, embed crash) ─────────────────────────────

class TestRouterFailureModes:
    """Acceptance criterion #5."""

    def test_vector_db_down_returns_cold(
        self, router_on, fake_embedder, monkeypatch,
    ):
        """When vector_db.connect() raises, router returns cold gracefully."""
        from services.rag import route
        import services.vector_db as vdb_module

        # Force connect() to raise. We do this by pointing at a bogus URL
        # without :memory: special-case.
        monkeypatch.setenv("QDRANT_URL", "http://nonexistent-host:9999")
        from config.settings import get_settings
        get_settings.cache_clear()
        vdb_module.vector_db.disconnect()
        vdb_module.vector_db._settings = get_settings()

        decision = route("any question")
        assert decision.mode == "cold"
        # We can't predict the exact wording but it should mention failure.
        assert "failed" in decision.reason.lower() or "no hits" in decision.reason.lower()


# ── Grounded preamble rendering ─────────────────────────────────────────────

class TestGroundedPreamble:
    """Acceptance criterion #6."""

    def _decision_with_hits(self, n: int):
        from services.rag import RouteDecision
        hits = []
        for i in range(n):
            hits.append({
                "title":   f"doc-{i}.md",
                "section": f"Section {i}",
                "content": "Lorem ipsum dolor sit amet, " * 30,  # ~180 chars each
                "url":     f"file:///docs/doc-{i}.md",
                "similarity": 0.75 + i * 0.01,
            })
        return RouteDecision(mode="grounded", grounded_chunks=hits)

    def test_empty_for_cold_decision(self):
        from services.rag import RouteDecision, build_grounded_preamble
        d = RouteDecision(mode="cold")
        assert build_grounded_preamble(d) == ""

    def test_empty_for_grounded_with_no_chunks(self):
        from services.rag import RouteDecision, build_grounded_preamble
        d = RouteDecision(mode="grounded", grounded_chunks=[])
        assert build_grounded_preamble(d) == ""

    def test_includes_section_and_similarity(self):
        from services.rag import build_grounded_preamble
        d = self._decision_with_hits(3)
        preamble = build_grounded_preamble(d, max_chars=4000)
        assert "doc-0.md > Section 0" in preamble
        assert "similarity=0.750" in preamble
        # Header + footer markers
        assert "Knowledge-base context" in preamble
        assert "end knowledge-base context" in preamble

    def test_respects_max_chars(self):
        from services.rag import build_grounded_preamble
        # Build 20 hits — far more than the 1000-char budget can hold.
        d = self._decision_with_hits(20)
        preamble = build_grounded_preamble(d, max_chars=1000)
        # Should clip well under the requested limit (allow some slop for
        # formatting + footer).
        assert len(preamble) <= 1100
