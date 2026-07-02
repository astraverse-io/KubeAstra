from __future__ import annotations

import logging

from alerts.domain.investigation import Investigation
from alerts.notifications.base import NotificationChannel

logger = logging.getLogger(__name__)


class LoggingNotificationChannel(NotificationChannel):
    name = "logging"

    async def send_investigation_summary(self, investigation: Investigation) -> None:
        logger.info(
            "investigation_summary_ready",
            extra={
                "investigation_id": investigation.investigation_id,
                "status": investigation.status,
                "rca": investigation.rca.summary if investigation.rca else None,
            },
        )


class NotificationDispatcher:
    def __init__(self, channels: list[NotificationChannel]) -> None:
        self.channels = channels

    async def send_summary(self, investigation: Investigation) -> None:
        for channel in self.channels:
            await channel.send_investigation_summary(investigation)
