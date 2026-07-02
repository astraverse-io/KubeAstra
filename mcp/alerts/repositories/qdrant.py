import asyncio
import logging
from alerts.domain.semantic import SemanticIncidentRecord
from alerts.repositories.base import SemanticMemoryRepository

logger = logging.getLogger(__name__)


class QdrantSemanticMemoryRepository(SemanticMemoryRepository):
    def __init__(self, client: object, collection_name: str) -> None:
        self.client = client
        self.collection_name = collection_name

    async def store(self, record: SemanticIncidentRecord) -> None:
        try:
            from services.embeddings import embeddings
            from services.rag.schema import INCIDENT_MEMORY
            from services.vector_db import VectorDB

            text = record.embedding_text()
            # Offload CPU-bound SentenceTransformers embed call to a worker thread
            vector = await asyncio.to_thread(embeddings.embed, text)

            payload = record.model_dump(mode="json")
            # Map canonical fields for general RAG search compliance
            payload["source"] = "incident_memory"
            payload["verified"] = True
            payload["title"] = f"RCA: {record.alert_name} ({record.investigation_id[:8]})"
            payload["timestamp"] = record.created_at.isoformat()
            
            # Ensure no duplication of payload fields (common fields already carry namespace/cluster)
            payload["namespace"] = record.namespace or "default"
            payload["cluster"] = record.cluster or "default"

            if isinstance(self.client, VectorDB):
                self.client.connect()
                self.client.ensure_collection_for(INCIDENT_MEMORY)
                self.client.upsert_point(
                    collection=self.collection_name,
                    point_id=record.investigation_id,
                    payload=payload,
                    vector=vector,
                )
            else:
                # Direct QdrantClient fallback (e.g. for testing)
                from qdrant_client.http import models as qmodels
                self.client.upsert(
                    collection_name=self.collection_name,
                    points=[
                        qmodels.PointStruct(
                            id=record.investigation_id,
                            vector=vector,
                            payload=payload,
                        )
                    ]
                )
        except Exception as exc:
            from alerts.shared.metrics import metrics
            metrics.increment("incident_memory_store_failures_total")
            logger.warning(
                f"Fail-soft: Qdrant incident memory store failed for investigation {record.investigation_id}: {exc}"
            )

    async def search(self, query: str, limit: int = 5) -> list[SemanticIncidentRecord]:
        # TODO: Implement recall in the orchestrator pipeline (Phase 4b).
        # Today's memory remains write-only in the main orchestration path.
        try:
            from services.embeddings import embeddings
            from services.vector_db import VectorDB

            # Offload CPU-bound SentenceTransformers embed call to a worker thread
            query_vector = await asyncio.to_thread(embeddings.embed, query)

            if isinstance(self.client, VectorDB):
                self.client.connect()
                hits = self.client.search_in(
                    collection=self.collection_name,
                    query_vector=query_vector,
                    limit=limit,
                )
            else:
                # Direct QdrantClient fallback (e.g. for testing)
                response = self.client.query_points(
                    collection_name=self.collection_name,
                    query=query_vector,
                    limit=limit,
                    with_payload=True,
                )
                hits = [
                    {**(hit.payload or {}), "id": str(hit.id)}
                    for hit in response.points
                ]

            records = []
            for hit in hits:
                try:
                    records.append(SemanticIncidentRecord.model_validate(hit))
                except Exception as val_err:
                    logger.warning(
                        f"Skipping malformed semantic record from Qdrant search results: {val_err}"
                    )
            return records
        except Exception as exc:
            logger.warning(f"Fail-soft: Qdrant incident memory search failed for query '{query}': {exc}")
            return []

