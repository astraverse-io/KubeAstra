from __future__ import annotations

from abc import ABC, abstractmethod

from alerts.domain.investigation import Investigation
from alerts.domain.semantic import SemanticIncidentRecord


class InvestigationRepository(ABC):
    @abstractmethod
    async def save(self, investigation: Investigation) -> None:
        pass

    @abstractmethod
    async def get(self, investigation_id: str) -> Investigation | None:
        pass


class SemanticMemoryRepository(ABC):
    @abstractmethod
    async def store(self, record: SemanticIncidentRecord) -> None:
        pass

    @abstractmethod
    async def search(self, query: str, limit: int = 5) -> list[SemanticIncidentRecord]:
        pass
