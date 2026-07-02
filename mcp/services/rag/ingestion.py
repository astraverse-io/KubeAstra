"""Ingestion orchestrator: discover → chunk → embed → upsert.

Idempotency: each chunk's UUID is derived deterministically from
SHA256(url + section + text). Re-running the pipeline against unchanged
docs is a no-op (exists() short-circuits before any embedding cost).

Failures inside a single document do not stop the run — they're logged
and counted in the returned stats so callers (or the CronJob) can alert.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterable

from services.embeddings import embeddings
from services.rag.chunking import pick_chunker
from services.rag.schema import CollectionSpec, DEVOPS_DOC
from services.rag.sources.base import Source
from services.vector_db import vector_db

logger = logging.getLogger(__name__)


@dataclass
class IngestStats:
    discovered: int = 0    # source documents seen
    chunks_seen: int = 0   # total chunks produced
    new: int = 0           # newly upserted
    skipped: int = 0       # already present (idempotent re-run)
    failed: int = 0        # chunks that errored (embed/upsert)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def merge(self, other: "IngestStats") -> None:
        self.discovered += other.discovered
        self.chunks_seen += other.chunks_seen
        self.new += other.new
        self.skipped += other.skipped
        self.failed += other.failed

    def to_dict(self) -> dict:
        return {
            "discovered": self.discovered,
            "chunks_seen": self.chunks_seen,
            "new": self.new,
            "skipped": self.skipped,
            "failed": self.failed,
            "duration_seconds": round((self.finished_at or time.time()) - self.started_at, 2),
        }


def ingest(
    source: Source,
    *,
    target: CollectionSpec = DEVOPS_DOC,
    max_tokens: int = 400,
    overlap_tokens: int = 60,
    dry_run: bool = False,
) -> IngestStats:
    """Ingest one source into ``target``. Creates the collection on first use."""
    stats = IngestStats()

    # Ensure the destination exists. Cheap if it already does.
    try:
        if not dry_run:
            vector_db.ensure_collection_for(target)
    except Exception as exc:
        logger.error("ingest: ensure_collection_for(%s) failed: %s", target.name, exc)
        stats.finished_at = time.time()
        return stats

    for doc in _safely_iterate(source):
        stats.discovered += 1
        # Pick the right chunker for this document's file_type — markdown,
        # Ansible tasks, playbooks, vars, templates, custom modules, or
        # the synthetic role_aggregate from ansible_roles.py.
        chunker = pick_chunker(doc)
        chunks = chunker(doc.content, doc.metadata or {}, max_tokens, overlap_tokens)
        if not chunks:
            continue

        # Batch embed per-document so we make one provider call per file
        # instead of one per chunk.
        try:
            texts_to_embed: list[str] = []
            idx_to_embed: list[int] = []
            ids: list[str] = []
            payloads: list[dict] = []

            # One timestamp per document — all chunks of the same file
            # share it. Cheaper than re-calling datetime.now() per chunk
            # and aligns chunk timestamps for a given file.
            doc_timestamp = _now_iso()

            # Pre-compute all chunk IDs and do ONE existence check for the
            # whole document rather than N (one per chunk). Cuts a 4k-chunk
            # incremental reindex from ~4k Qdrant roundtrips to ~1.2k
            # (one per doc).
            chunk_payloads: list[tuple[str, str, dict]] = []  # (id, text, payload)
            for chunk in chunks:
                stats.chunks_seen += 1
                chunk_hash = _hash_chunk(doc.url, chunk.section, chunk.text)
                point_id = _uuid_from_hash(chunk_hash)
                payload = {
                    **(doc.metadata or {}),
                    # Chunker-specific fields (task_name, module, has_rescue,
                    # play_name, hosts, roles_list, template_path,
                    # module_name, kind, etc.) — merged after metadata so
                    # they win on key collisions like ``kind``.
                    **(chunk.extra or {}),
                    "title": doc.title,
                    "url": doc.url,
                    "content": chunk.text,
                    "section": chunk.section,
                    "chunk_index": chunk.index,
                    "chunk_hash": chunk_hash,
                    "timestamp": doc_timestamp,
                    "user": "system",
                    "verified": True,   # ingested team docs are trusted by default
                    "upvotes": 0,
                }
                chunk_payloads.append((point_id, chunk.text, payload))

            already_present: set[str] = set()
            if not dry_run and chunk_payloads:
                already_present = vector_db.exists_many(
                    target.name, [pid for pid, _, _ in chunk_payloads],
                )

            for point_id, text, payload in chunk_payloads:
                local_pos = len(ids)
                ids.append(point_id)
                payloads.append(payload)
                if point_id in already_present:
                    stats.skipped += 1
                    continue
                texts_to_embed.append(text)
                idx_to_embed.append(local_pos)

            if dry_run:
                continue
            if not texts_to_embed:
                continue

            vecs = embeddings.embed_many(texts_to_embed)
            for rel_pos, vec in zip(idx_to_embed, vecs):
                try:
                    vector_db.upsert_point(
                        collection=target.name,
                        point_id=ids[rel_pos],
                        payload=payloads[rel_pos],
                        vector=vec,
                    )
                    stats.new += 1
                except Exception as exc:
                    stats.failed += 1
                    logger.warning("ingest upsert failed for %s [%s]: %s",
                                   doc.url, ids[rel_pos][:8], exc)
        except Exception as exc:
            # Whole-document failure (e.g. embed batch raised) — count
            # remaining chunks as failed so stats stay accurate.
            failed_now = len(chunks) - stats.skipped - stats.new
            stats.failed += max(0, failed_now)
            logger.warning("ingest doc failed for %s: %s", doc.url, exc)

    stats.finished_at = time.time()
    logger.info("ingest finished: %s", stats.to_dict())
    return stats


def ingest_many(
    sources: Iterable[Source],
    *,
    target: CollectionSpec = DEVOPS_DOC,
    max_tokens: int = 400,
    overlap_tokens: int = 60,
    dry_run: bool = False,
) -> IngestStats:
    """Convenience: ingest several sources in sequence, return combined stats."""
    overall = IngestStats()
    for src in sources:
        stats = ingest(src, target=target, max_tokens=max_tokens,
                       overlap_tokens=overlap_tokens, dry_run=dry_run)
        overall.merge(stats)
    overall.finished_at = time.time()
    return overall


# ── Helpers ──────────────────────────────────────────────────────────────────

def _safely_iterate(source: Source):
    """Iterate a source, swallowing per-item exceptions so one bad doc
    doesn't kill the whole run."""
    try:
        yield from source.discover()
    except Exception as exc:
        logger.error("source %s discover() raised: %s", getattr(source, "name", "?"), exc)


def _hash_chunk(url: str, section: str, text: str) -> str:
    h = hashlib.sha256()
    h.update(url.encode("utf-8")); h.update(b"|")
    h.update(section.encode("utf-8")); h.update(b"|")
    h.update(text.encode("utf-8"))
    return h.hexdigest()


def _uuid_from_hash(hex_digest: str) -> str:
    """Qdrant accepts UUIDs and ints as point IDs; we use UUID5 over the
    SHA256 so equal hashes → equal UUIDs (idempotent)."""
    return str(uuid.UUID(hex=hex_digest[:32]))


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
