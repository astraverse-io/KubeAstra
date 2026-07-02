"""Qdrant vector DB service for RAG-based similar-error lookup.

This module replaces the previous Weaviate-backed implementation. The
public surface (``connect``, ``search``, ``add``, ``disconnect``) is
preserved so existing callers (``ai_tools/analyze.py``, ``data/seed.py``)
keep working without modification. Phase 1.2 will extend with
multi-collection support for ingested docs + runbooks + session memory.

Why Qdrant over Weaviate: the prior wrapper used only the generic
near-vector search + properties API (no modules, no GraphQL, no
multi-tenancy). Qdrant offers the same scale class with ~5-10x less
memory at idle, first-class payload filters, simpler Python client,
and built-in snapshots — a better fit for self-hosted in-cluster
operation by a DevOps team. See ``docs/AGENT_FEATURE_ROADMAP.md``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from config.settings import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class VectorDB:
    """Thin Qdrant wrapper exposing the legacy add/search/connect API."""

    def __init__(self):
        self._client: Optional[Any] = None
        self.collection = settings.qdrant_collection
        self._vector_size = settings.embedding_dim

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        # Idempotent: cheap to call from many sites (analyze.py, kb_search,
        # ingestion). Recreating the client would also wipe in-memory Qdrant
        # state in tests; on a real server it's just wasteful.
        if self._client is not None:
            return

        try:
            from qdrant_client import QdrantClient
            from qdrant_client.http import models as qmodels
        except ImportError as exc:  # pragma: no cover — surfaced at startup
            raise RuntimeError(
                "qdrant-client is not installed. Add it to requirements.txt "
                "and re-run setup."
            ) from exc

        kwargs: dict = {"url": settings.qdrant_url, "timeout": settings.qdrant_timeout_seconds}
        if settings.qdrant_api_key:
            kwargs["api_key"] = settings.qdrant_api_key

        self._client = QdrantClient(**kwargs)
        self._ensure_collection(qmodels)
        logger.info(
            "VectorDB connected url=%s collection=%s vector_size=%d",
            settings.qdrant_url, self.collection, self._vector_size,
        )

    def disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception as exc:
                logger.debug("VectorDB close raised (ignored): %s", exc)
            self._client = None

    # ── Schema bootstrap ─────────────────────────────────────────────────────

    def _ensure_collection(self, qmodels) -> None:
        """Create the collection if it doesn't exist. Idempotent."""
        try:
            existing = {c.name for c in self._client.get_collections().collections}
        except Exception as exc:
            logger.warning("VectorDB: get_collections failed: %s", exc)
            existing = set()

        if self.collection in existing:
            return

        self._client.create_collection(
            collection_name=self.collection,
            vectors_config=qmodels.VectorParams(
                size=self._vector_size,
                # Embeddings (sentence-transformers/all-MiniLM-L6-v2) are
                # already L2-normalized in embeddings.py, so cosine is the
                # right metric.
                distance=qmodels.Distance.COSINE,
            ),
        )

        # Index commonly-filtered payload fields so search-with-filter is fast.
        for field in ("tool", "category", "severity"):
            try:
                self._client.create_payload_index(
                    collection_name=self.collection,
                    field_name=field,
                    field_schema=qmodels.PayloadSchemaType.KEYWORD,
                )
            except Exception as exc:
                logger.debug("payload index for %s failed (ok if exists): %s", field, exc)

        logger.info("Created Qdrant collection: %s", self.collection)

    # ── Write ────────────────────────────────────────────────────────────────

    def add(
        self,
        error_text: str,
        tool: str,
        category: str,
        solution_text: str,
        commands: str,
        success_rate: float,
        severity: str,
        vector: list[float],
    ) -> None:
        if self._client is None:
            raise RuntimeError("VectorDB.add called before connect()")

        from qdrant_client.http import models as qmodels
        import uuid as _uuid

        payload = {
            "error_text":    error_text,
            "tool":          tool,
            "category":      category,
            "solution_text": solution_text,
            "commands":      commands,
            "success_rate":  success_rate,
            "severity":      severity,
        }

        self._client.upsert(
            collection_name=self.collection,
            points=[
                qmodels.PointStruct(
                    id=str(_uuid.uuid4()),
                    vector=vector,
                    payload=payload,
                )
            ],
        )

    # ── Read ─────────────────────────────────────────────────────────────────

    # ── Multi-collection helpers (Phase 1.2) ────────────────────────────────
    # Used by services/rag/* for ingested docs, runbooks, session memory.
    # Keep the legacy connect/add/search above untouched so existing
    # callers (ai_tools/analyze.py, data/seed.py) work unchanged.

    def ensure_collection_for(self, spec) -> None:
        """Create the named Qdrant collection + indexes if missing.

        ``spec`` is a ``services.rag.schema.CollectionSpec`` (imported
        lazily to avoid a circular import).
        """
        if self._client is None:
            raise RuntimeError("VectorDB.ensure_collection_for called before connect()")
        from qdrant_client.http import models as qmodels

        try:
            existing = {c.name for c in self._client.get_collections().collections}
        except Exception as exc:
            logger.warning("VectorDB: get_collections failed: %s", exc)
            existing = set()

        if spec.name in existing:
            # Existing collection — ensure its indexing_threshold is low
            # enough that small corpora (under 20K vectors) get a usable
            # HNSW index. Qdrant's default 20K means a fresh deployment-
            # repo install (~4K vectors) returns 0 points from search
            # until either the threshold is patched or more vectors land.
            # Idempotent: safe to call every connect().
            try:
                self._client.update_collection(
                    collection_name=spec.name,
                    optimizers_config=qmodels.OptimizersConfigDiff(
                        indexing_threshold=1000,
                    ),
                )
            except Exception as exc:
                logger.debug(
                    "Could not patch indexing_threshold on %s: %s "
                    "(ok if already set)", spec.name, exc,
                )
            return

        self._client.create_collection(
            collection_name=spec.name,
            vectors_config=qmodels.VectorParams(
                size=self._vector_size,
                distance=qmodels.Distance.COSINE,
            ),
            # indexing_threshold=1000 (vs Qdrant's default 20K) so small
            # corpora — common for team RAG — actually get indexed and
            # become searchable. Without this, a freshly-ingested
            # collection has points_count=N but indexed_vectors_count=0,
            # and every search returns no hits despite the data being
            # there. Setting on create AND in the existing-collection
            # branch above so both fresh installs and upgrades self-heal.
            optimizers_config=qmodels.OptimizersConfigDiff(
                indexing_threshold=1000,
            ),
        )
        for field_name in spec.indexed_fields:
            try:
                self._client.create_payload_index(
                    collection_name=spec.name,
                    field_name=field_name,
                    field_schema=qmodels.PayloadSchemaType.KEYWORD,
                )
            except Exception as exc:
                logger.debug("payload index for %s.%s failed (ok if exists): %s",
                             spec.name, field_name, exc)
        logger.info("Created Qdrant collection: %s", spec.name)

    def upsert_point(
        self,
        collection: str,
        point_id: str,
        payload: dict,
        vector: list[float],
    ) -> None:
        """Idempotent upsert by ``point_id`` (use a content-hash UUID for
        idempotent re-ingest)."""
        if self._client is None:
            raise RuntimeError("VectorDB.upsert_point called before connect()")
        from qdrant_client.http import models as qmodels
        self._client.upsert(
            collection_name=collection,
            points=[qmodels.PointStruct(id=point_id, vector=vector, payload=payload)],
        )

    def exists(self, collection: str, point_id: str) -> bool:
        """Cheap check used by the ingestion pipeline for incremental sync."""
        if self._client is None:
            return False
        try:
            pts = self._client.retrieve(
                collection_name=collection,
                ids=[point_id],
                with_payload=False,
                with_vectors=False,
            )
            return bool(pts)
        except Exception:
            return False

    def exists_many(self, collection: str, point_ids: list[str]) -> set[str]:
        """Batch existence check — returns the subset of ``point_ids`` that
        already exist in the collection. One Qdrant roundtrip total
        instead of N. Used by the ingestion pipeline to make incremental
        reindex cheap, which matters on real (non-loopback) deployments
        where each round-trip is milliseconds.

        Empty input → empty set. Errors → empty set (caller treats every
        ID as missing and will upsert; idempotent UUIDs make this safe).
        """
        if self._client is None or not point_ids:
            return set()
        try:
            pts = self._client.retrieve(
                collection_name=collection,
                ids=point_ids,
                with_payload=False,
                with_vectors=False,
            )
            return {str(p.id) for p in pts}
        except Exception as exc:
            logger.debug("VectorDB.exists_many failed: %s", exc)
            return set()

    def search_in(
        self,
        collection: str,
        query_vector: list[float],
        filters: Optional[dict] = None,
        limit: int = 5,
        log_failures: bool = True,
    ) -> list[dict]:
        """Generic search against any collection with simple equality filters.

        ``filters`` is a dict of ``{field: value}`` pairs combined with AND.
        Returns ``[{payload..., id, similarity}, ...]``.
        """
        if self._client is None:
            return []
        try:
            from qdrant_client.http import models as qmodels

            query_filter = None
            if filters:
                conds = [
                    qmodels.FieldCondition(key=k, match=qmodels.MatchValue(value=v))
                    for k, v in filters.items() if v is not None
                ]
                if conds:
                    query_filter = qmodels.Filter(must=conds)

            response = self._client.query_points(
                collection_name=collection,
                query=query_vector,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
            return [
                {**(hit.payload or {}), "id": str(hit.id),
                 "similarity": round(float(hit.score), 3)}
                for hit in response.points
            ]
        except Exception as exc:
            log = logger.warning if log_failures else logger.debug
            log("Vector search in %s failed: %s", collection, exc)
            return []

    # ── Legacy K8sError search (kept verbatim for backward compat) ──────────

    def search(
        self,
        query_vector: list[float],
        tool: Optional[str] = None,
        limit: int = 5,
    ) -> list[dict]:
        """Nearest-neighbour search with optional tool-name filter.

        Mirrors the prior Weaviate-era return shape so callers don't change.
        """
        if self._client is None:
            return []

        try:
            from qdrant_client.http import models as qmodels

            query_filter = None
            if tool:
                # Match the historical semantics: callers pass tool="kubernetes"
                # or "ansible"; entries flagged "both" should match either.
                query_filter = qmodels.Filter(
                    should=[
                        qmodels.FieldCondition(key="tool", match=qmodels.MatchValue(value=tool)),
                        qmodels.FieldCondition(key="tool", match=qmodels.MatchValue(value="both")),
                    ]
                )

            # qdrant-client ≥ 1.10 uses query_points; .search() was removed in 1.12+.
            response = self._client.query_points(
                collection_name=self.collection,
                query=query_vector,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
            hits = response.points
        except Exception as exc:
            logger.warning("Vector search failed: %s", exc)
            return []

        results: list[dict] = []
        for hit in hits:
            p = hit.payload or {}
            results.append({
                "error_text":    p.get("error_text", ""),
                "tool":          p.get("tool", ""),
                "category":      p.get("category", ""),
                "solution_text": p.get("solution_text", ""),
                "commands":      p.get("commands", ""),
                "success_rate":  p.get("success_rate", 0),
                "severity":      p.get("severity", "medium"),
                # Qdrant's cosine "score" is already a similarity in [-1, 1];
                # higher is better. Round for stable rendering.
                "similarity":    round(float(hit.score), 3),
            })
        return results


vector_db = VectorDB()
