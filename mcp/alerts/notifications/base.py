from __future__ import annotations

from abc import ABC, abstractmethod

from alerts.domain.investigation import Investigation


class NotificationChannel(ABC):
    name: str

    @abstractmethod
    async def send_investigation_summary(self, investigation: Investigation) -> None:
        pass
