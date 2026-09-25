"""Tests for the Qdrant VectorDB wrapper (blueprint §6).

Uses Qdrant's `:memory:` mode — no server, no network, deterministic.

Covers the acceptance criteria from the blueprint:
  1. `connect()` called twice is idempotent (no-op second time).
  2. `ensure_collection_for(spec)` is idempotent (no-op when collection exists).
  3. `upsert_point` with the same point_id is idempotent (1 point, not 2).
  4. `search_in` with filters returns only matching payloads, sorted by similarity desc.
  5. `search_in` against missing collection returns [], never raises.
  6. `:memory:` mode works (used here for the whole suite).
  7. Helm StatefulSet manifest renders to valid YAML (covered by Helm dry-run separately).
"""

import os
import sys
import uuid

import pytest

MCP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)


@pytest.fixture
def memory_qdrant(monkeypatch):
    """Point the VectorDB singleton at an `:memory:` Qdrant instance.

    Uses qdrant-client's built-in in-process mode — no real server,
    no network. State is per-test (new VectorDB() each fixture run).
    """
    monkeypatch.setenv("QDRANT_URL", ":memory:")
    monkeypatch.setenv("QDRANT_COLLECTION", "k8s_errors_test")
    monkeypatch.setenv("EMBEDDING_DIM", "8")  # small dim → fast tests

    # Force settings cache refresh
    from config.settings import get_settings
    get_settings.cache_clear()

    # Build a fresh VectorDB so it picks up the new settings (the module-
    # level singleton was created with the original env).
    from services.vector_db import VectorDB
    db = VectorDB()
    db.connect()
    yield db
    db.disconnect()
    get_settings.cache_clear()


def _make_vec(seed: int, dim: int = 8) -> list[float]:
    """Build a deterministic unit-ish vector for tests."""
    return [(seed + i) / 10.0 for i in range(dim)]


def _new_point_id() -> str:
    return str(uuid.uuid4())


# ── Connection lifecycle ─────────────────────────────────────────────────────

class TestConnect:

    def test_connect_is_idempotent(self, memory_qdrant):
        # Already connected by the fixture; second call must not raise.
        memory_qdrant.connect()
        memory_qdrant.connect()
        assert memory_qdrant.is_connected is True

    def test_disconnect_then_reconnect(self, memory_qdrant):
        memory_qdrant.disconnect()
        assert memory_qdrant.is_connected is False
        # NOTE: :memory: Qdrant doesn't persist across disconnect → reconnect.
        # We just verify the lifecycle doesn't crash.
        memory_qdrant.connect()
        assert memory_qdrant.is_connected is True


# ── Collection management ────────────────────────────────────────────────────

class TestCollections:

    def test_ensure_collection_creates_when_missing(self, memory_qdrant):
        from services.vector_db import CollectionSpec
        spec = CollectionSpec(
            name="test_runbook",
            indexed_fields=("cluster", "verified"),
        )
        memory_qdrant.ensure_collection_for(spec)
        assert memory_qdrant.collection_exists("test_runbook")

    def test_ensure_collection_is_idempotent(self, memory_qdrant):
        from services.vector_db import CollectionSpec
        spec = CollectionSpec(name="test_runbook")
        memory_qdrant.ensure_collection_for(spec)
        # Second call must not raise — collection already exists.
        memory_qdrant.ensure_collection_for(spec)
        assert memory_qdrant.collection_exists("test_runbook")


# ── Upsert + exists ──────────────────────────────────────────────────────────

class TestUpsert:

    def test_upsert_creates_point(self, memory_qdrant):
        from services.vector_db import CollectionSpec
        memory_qdrant.ensure_collection_for(CollectionSpec(name="t1"))
        pid = _new_point_id()
        memory_qdrant.upsert_point(
            collection="t1",
            point_id=pid,
            payload={"title": "hello", "tool": "kubernetes"},
            vector=_make_vec(1),
        )
        assert memory_qdrant.exists("t1", pid)

    def test_upsert_is_idempotent_same_id(self, memory_qdrant):
        """Re-upsert with same id mutates the existing point, doesn't duplicate."""
        from services.vector_db import CollectionSpec
        memory_qdrant.ensure_collection_for(CollectionSpec(name="t2"))
        pid = _new_point_id()
        memory_qdrant.upsert_point(
            collection="t2", point_id=pid,
            payload={"v": 1}, vector=_make_vec(1),
        )
        memory_qdrant.upsert_point(
            collection="t2", point_id=pid,
            payload={"v": 2}, vector=_make_vec(2),
        )
        # Search should return exactly 1 point with the updated payload.
        results = memory_qdrant.search_in("t2", _make_vec(2), limit=10)
        assert len(results) == 1
        assert results[0]["v"] == 2

    def test_exists_false_for_unknown_point(self, memory_qdrant):
        from services.vector_db import CollectionSpec
        memory_qdrant.ensure_collection_for(CollectionSpec(name="t3"))
        assert memory_qdrant.exists("t3", _new_point_id()) is False


# ── Search ───────────────────────────────────────────────────────────────────

class TestSearch:

    def test_search_returns_sorted_by_similarity(self, memory_qdrant):
        """Closer vectors come first."""
        from services.vector_db import CollectionSpec
        memory_qdrant.ensure_collection_for(CollectionSpec(name="ts1"))

        memory_qdrant.upsert_point(
            collection="ts1", point_id=_new_point_id(),
            payload={"label": "close"}, vector=_make_vec(1),
        )
        memory_qdrant.upsert_point(
            collection="ts1", point_id=_new_point_id(),
            payload={"label": "far"}, vector=_make_vec(99),
        )

        hits = memory_qdrant.search_in("ts1", _make_vec(1), limit=10)
        assert len(hits) == 2
        # First hit is the closer vector
        assert hits[0]["label"] == "close"
        # Similarity scores are descending
        assert hits[0]["similarity"] >= hits[1]["similarity"]

    def test_search_with_filter_returns_only_matching(self, memory_qdrant):
        """Equality filter on payload restricts the result set."""
        from services.vector_db import CollectionSpec
        spec = CollectionSpec(name="ts2", indexed_fields=("namespace",))
        memory_qdrant.ensure_collection_for(spec)

        memory_qdrant.upsert_point(
            collection="ts2", point_id=_new_point_id(),
            payload={"namespace": "prod", "name": "a"},
            vector=_make_vec(1),
        )
        memory_qdrant.upsert_point(
            collection="ts2", point_id=_new_point_id(),
            payload={"namespace": "staging", "name": "b"},
            vector=_make_vec(1),
        )

        hits = memory_qdrant.search_in(
            "ts2", _make_vec(1), filters={"namespace": "prod"}, limit=10,
        )
        assert len(hits) == 1
        assert hits[0]["name"] == "a"
        assert hits[0]["namespace"] == "prod"

    def test_search_skips_wildcard_filters(self, memory_qdrant):
        """Filter value of '*' / None / '' means 'don't filter on this key'."""
        from services.vector_db import CollectionSpec
        memory_qdrant.ensure_collection_for(
            CollectionSpec(name="ts3", indexed_fields=("namespace",))
        )

        memory_qdrant.upsert_point(
            collection="ts3", point_id=_new_point_id(),
            payload={"namespace": "prod"}, vector=_make_vec(1),
        )

        # All three forms should NOT filter — they're "no constraint" markers.
        for wildcard in ("*", None, ""):
            hits = memory_qdrant.search_in(
                "ts3", _make_vec(1),
                filters={"namespace": wildcard},
                limit=10,
            )
            assert len(hits) == 1, f"wildcard={wildcard!r} unexpectedly filtered out the point"

    def test_search_missing_collection_returns_empty(self, memory_qdrant):
        """Search against a non-existent collection must NOT raise."""
        hits = memory_qdrant.search_in("does_not_exist", _make_vec(1), limit=5)
        assert hits == []

    def test_search_empty_collection_returns_empty(self, memory_qdrant):
        from services.vector_db import CollectionSpec
        memory_qdrant.ensure_collection_for(CollectionSpec(name="ts4"))
        hits = memory_qdrant.search_in("ts4", _make_vec(1), limit=5)
        assert hits == []


# ── Legacy back-compat shims (analyze.py, seed.py) ───────────────────────────

class TestLegacyCompat:

    def test_legacy_add_then_search(self, memory_qdrant):
        """Old API still works: vector_db.add() inserts, vector_db.search() finds it."""
        memory_qdrant.add(
            error_text="OOMKilled - exceeded memory limit",
            tool="kubernetes",
            category="pod_oom",
            solution_text="Bump memory limit",
            commands="kubectl edit deployment ...",
            success_rate=95.0,
            severity="high",
            vector=_make_vec(1),
        )

        # search() uses the default collection (k8s_errors_test in fixture)
        hits = memory_qdrant.search(_make_vec(1), tool="kubernetes", limit=5)
        assert len(hits) == 1
        assert hits[0]["category"] == "pod_oom"
        assert "memory" in hits[0]["solution_text"].lower()

    def test_legacy_add_is_idempotent(self, memory_qdrant):
        """Re-seeding the same error text doesn't duplicate the point."""
        for _ in range(3):
            memory_qdrant.add(
                error_text="Identical error",
                tool="kubernetes", category="x",
                solution_text="y", commands="z",
                success_rate=50.0, severity="low",
                vector=_make_vec(1),
            )
        hits = memory_qdrant.search(_make_vec(1), tool="kubernetes", limit=10)
        assert len(hits) == 1
