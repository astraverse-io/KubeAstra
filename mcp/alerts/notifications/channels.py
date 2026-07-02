from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass

from alerts.domain.investigation import Investigation
from alerts.notifications.base import NotificationChannel

logger = logging.getLogger(__name__)

# Slack section text limit is 3000 chars; keep some headroom for prefixes.
_SLACK_TIMEOUT_SECS = 8
_MAX_SLACK_TEXT = 2_800
_SEVERITY_EMOJI = {
    "critical": "🔴",
    "high":     "🔴",
    "warning":  "🟠",
    "medium":   "🟠",
    "low":      "🔵",
    "info":     "🔵",
}


@dataclass(frozen=True)
class WebhookNotificationConfig:
    webhook_url: str | None = None
    routing_key: str | None = None
    email_from: str | None = None
    email_to: tuple[str, ...] = ()


def _session_url(investigation_id: str) -> str:
    """Deep link to the shareable investigation, if PUBLIC_BASE_URL is set."""
    base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base and investigation_id:
        return f"{base}/investigations/{investigation_id}"
    return ""


def _severity_emoji(alert_severity: str, has_rca: bool) -> str:
    """Best-effort severity → emoji, with defensive lowercase lookup."""
    key = (alert_severity or "").strip().lower()
    return _SEVERITY_EMOJI.get(key, "🔍" if has_rca else "⚠️")


def build_slack_payload(investigation: Investigation) -> dict:
    """Build a Slack blocks payload from an investigation.

    Layout:
      - Header line: severity emoji + "KubeAstra investigation"
      - Alert title (labels/name from the firing alert)
      - RCA summary (truncated to Slack's section limit)
      - Context: alert source · optional deep link
    """
    alert = investigation.alert
    rca = investigation.rca
    tool_used = investigation.selected_playbook or "-"

    emoji = _severity_emoji(alert.severity, has_rca=rca is not None)
    title = f"{emoji} KubeAstra investigation"

    alert_line = alert.name
    ns = alert.labels.get("namespace") if alert.labels else None
    if ns:
        alert_line = f"{alert.name} · `{ns}`"

    if rca and rca.summary:
        body = rca.summary[:_MAX_SLACK_TEXT]
        if rca.recommendations:
            recs = "\n".join(f"• {r}" for r in rca.recommendations[:3])
            # Keep total body under the section limit.
            joined = f"{body}\n\n*Recommended actions:*\n{recs}"
            body = joined[:_MAX_SLACK_TEXT]
    else:
        body = "_Investigation completed without a root-cause summary._"

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": title}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{alert_line[:280]}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
    ]

    context_bits = [f"playbook: `{tool_used}`", f"source: `{alert.source.value}`"]
    link = _session_url(investigation.investigation_id)
    if link:
        context_bits.append(f"<{link}|open investigation>")
    blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "  ·  ".join(context_bits)}]}
    )

    return {
        "text": f"{title}: {alert_line[:200]}",  # plain-text fallback for notifications
        "blocks": blocks,
    }


def _post_slack(webhook_url: str, payload: dict) -> None:
    """POST payload to a Slack incoming webhook. Raises on transport error.

    Uses stdlib urllib for the single small POST rather than adding an
    async HTTP client to the alerts subsystem. The caller is responsible
    for swallowing exceptions so a Slack outage cannot fail the
    investigation.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=_SLACK_TIMEOUT_SECS) as resp:
        resp.read()


class SlackNotificationChannel(NotificationChannel):
    """Post a Slack blocks message per completed investigation.

    Behavior:
      - Always emits a structured log line (safe fallback + audit trail)
      - When ``config.webhook_url`` is set, POSTs a Slack blocks payload
        via stdlib urllib
      - Never raises: Slack failures are logged and swallowed so the
        alert orchestrator's background thread is unaffected
    """
    name = "slack"

    def __init__(self, config: WebhookNotificationConfig) -> None:
        self.config = config

    async def send_investigation_summary(self, investigation: Investigation) -> None:
        logger.info("slack_notification_prepared", extra=self._extra(investigation))

        webhook_url = (self.config.webhook_url or "").strip()
        if not webhook_url:
            return

        try:
            payload = build_slack_payload(investigation)
            _post_slack(webhook_url, payload)
            logger.info(
                "slack_notify_ok",
                extra={
                    "channel": self.name,
                    "investigation_id": investigation.investigation_id,
                },
            )
        except Exception:
            logger.warning(
                "slack_notify_failed",
                extra={
                    "channel": self.name,
                    "investigation_id": investigation.investigation_id,
                },
                exc_info=True,
            )

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
