from __future__ import annotations

import logging
from dataclasses import dataclass

from alerts.domain.investigation import Investigation
from alerts.notifications.base import NotificationChannel

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebhookNotificationConfig:
    webhook_url: str | None = None
    routing_key: str | None = None
    email_from: str | None = None
    email_to: tuple[str, ...] = ()


class SlackNotificationChannel(NotificationChannel):
    name = "slack"

    def __init__(self, config: WebhookNotificationConfig) -> None:
        self.config = config

    async def send_investigation_summary(self, investigation: Investigation) -> None:
        logger.info("slack_notification_prepared", extra=self._extra(investigation))

    def _extra(self, investigation: Investigation) -> dict:
        return {
            "channel": self.name,
            "investigation_id": investigation.investigation_id,
            "webhook_configured": bool(self.config.webhook_url),
        }


class PagerDutyNotificationChannel(NotificationChannel):
    name = "pagerduty"

    def __init__(self, config: WebhookNotificationConfig) -> None:
        self.config = config

    async def send_investigation_summary(self, investigation: Investigation) -> None:
        logger.info(
            "pagerduty_notification_prepared",
            extra={
                "channel": self.name,
                "investigation_id": investigation.investigation_id,
                "routing_key_configured": bool(self.config.routing_key),
            },
        )


class EmailNotificationChannel(NotificationChannel):
    name = "email"

    def __init__(self, config: WebhookNotificationConfig) -> None:
        self.config = config

    async def send_investigation_summary(self, investigation: Investigation) -> None:
        logger.info(
            "email_notification_prepared",
            extra={
                "channel": self.name,
                "investigation_id": investigation.investigation_id,
                "recipient_count": len(self.config.email_to),
            },
        )


class TeamsNotificationChannel(NotificationChannel):
    name = "teams"

    def __init__(self, config: WebhookNotificationConfig) -> None:
        self.config = config

    async def send_investigation_summary(self, investigation: Investigation) -> None:
        logger.info(
            "teams_notification_prepared",
            extra={
                "channel": self.name,
                "investigation_id": investigation.investigation_id,
                "webhook_configured": bool(self.config.webhook_url),
            },
        )
