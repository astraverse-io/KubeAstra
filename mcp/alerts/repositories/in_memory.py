from __future__ import annotations

from alerts.domain.investigation import Investigation
from alerts.domain.semantic import SemanticIncidentRecord
from alerts.repositories.base import InvestigationRepository, SemanticMemoryRepository


class InMemoryInvestigationRepository(InvestigationRepository):
    def __init__(self) -> None:
        self._items: dict[str, Investigation] = {}

    async def save(self, investigation: Investigation) -> None:
        self._items[investigation.investigation_id] = investigation.model_copy(deep=True)

    async def get(self, investigation_id: str) -> Investigation | None:
        item = self._items.get(investigation_id)
        return item.model_copy(deep=True) if item else None


class InMemorySemanticMemoryRepository(SemanticMemoryRepository):
    def __init__(self) -> None:
        self._items: list[SemanticIncidentRecord] = []

    async def store(self, record: SemanticIncidentRecord) -> None:
        self._items.append(record)

    async def search(self, query: str, limit: int = 5) -> list[SemanticIncidentRecord]:
        query_lower = query.lower()
        matches = [item for item in self._items if query_lower in item.embedding_text().lower()]
        return matches[:limit]
