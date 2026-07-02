"""Slack notification channel tests.

Verifies the SlackNotificationChannel: payload shape, severity → emoji
mapping, PUBLIC_BASE_URL deep-linking, missing-webhook no-op, and the
critical never-raises contract for Slack failures.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from alerts.domain.alert import Alert  # noqa: E402
from alerts.domain.investigation import Investigation  # noqa: E402
from alerts.domain.rca import RootCauseAnalysis  # noqa: E402
from alerts.notifications.channels import (  # noqa: E402
    SlackNotificationChannel,
    WebhookNotificationConfig,
    build_slack_payload,
)


def _make_alert(severity: str = "critical", namespace: str = "payments") -> Alert:
    return Alert.from_parts(
        name="KubePodCrashLooping",
        source="alertmanager",
        severity=severity,
        labels={"namespace": namespace, "pod": "checkout-0"},
        raw_payload={},
    )


def _make_investigation(
    severity: str = "critical",
    rca_summary: str = "Redis PVC is unbound, blocking startup.",
    recommendations: list[str] | None = None,
) -> Investigation:
    inv = Investigation(alert=_make_alert(severity=severity))
    inv.rca = RootCauseAnalysis(
        summary=rca_summary,
        confidence=0.85,
        root_causes=["redis-data PVC unbound"],
        recommendations=recommendations if recommendations is not None else ["Provision the PVC", "Restart Redis"],
    )
    return inv


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ── build_slack_payload ─────────────────────────────────────────────────────


def test_payload_header_uses_severity_emoji():
    payload = build_slack_payload(_make_investigation(severity="critical"))
    assert payload["blocks"][0]["type"] == "header"
    assert "🔴" in payload["blocks"][0]["text"]["text"]


def test_payload_falls_back_to_search_emoji_when_severity_missing():
    payload = build_slack_payload(_make_investigation(severity="unknown"))
    assert "🔍" in payload["blocks"][0]["text"]["text"]


def test_payload_alert_line_includes_namespace_when_present():
    payload = build_slack_payload(_make_investigation())
    alert_block = payload["blocks"][1]
    assert alert_block["type"] == "section"
    assert "KubePodCrashLooping" in alert_block["text"]["text"]
    assert "`payments`" in alert_block["text"]["text"]


def test_payload_rca_summary_and_recommendations_render():
    payload = build_slack_payload(_make_investigation())
    body = payload["blocks"][2]["text"]["text"]
    assert "Redis PVC is unbound" in body
    assert "Recommended actions:" in body
    assert "• Provision the PVC" in body
    assert "• Restart Redis" in body


def test_payload_truncates_long_rca_summary_under_slack_limit():
    huge = "x" * 5000
    payload = build_slack_payload(_make_investigation(rca_summary=huge, recommendations=[]))
    body_text = payload["blocks"][2]["text"]["text"]
    assert len(body_text) <= 2_800


def test_payload_handles_missing_rca_gracefully():
    inv = Investigation(alert=_make_alert())
    inv.rca = None
    payload = build_slack_payload(inv)
    body = payload["blocks"][2]["text"]["text"]
    assert "without a root-cause summary" in body


def test_payload_appends_deep_link_when_public_base_url_set(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://kubeastra.example.com")
    payload = build_slack_payload(_make_investigation())
    context = payload["blocks"][-1]
    assert context["type"] == "context"
    text = context["elements"][0]["text"]
    assert "open investigation" in text
    # /alerts is the actual frontend route (list + sidebar drill-down); the
    # `investigation` query param auto-opens the row. There is no
    # /investigations/<id> route.
    assert "https://kubeastra.example.com/alerts?investigation=inv_" in text


def test_payload_omits_deep_link_without_public_base_url(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    payload = build_slack_payload(_make_investigation())
    context = payload["blocks"][-1]
    assert "open investigation" not in context["elements"][0]["text"]


def test_payload_includes_source_and_playbook_in_context():
    inv = _make_investigation()
    inv.selected_playbook = "crashloop_recovery"
    payload = build_slack_payload(inv)
    context_text = payload["blocks"][-1]["elements"][0]["text"]
    assert "playbook: `crashloop_recovery`" in context_text
    assert "source: `alertmanager`" in context_text


def test_payload_fallback_text_for_notifications():
    payload = build_slack_payload(_make_investigation())
    assert payload["text"].startswith("🔴 KubeAstra investigation")
    assert "KubePodCrashLooping" in payload["text"]


# ── SlackNotificationChannel ────────────────────────────────────────────────


def test_channel_no_op_without_webhook():
    """When no webhook URL is configured, the channel only logs — never POSTs."""
    channel = SlackNotificationChannel(WebhookNotificationConfig())
    with patch("alerts.notifications.channels._post_slack") as fake_post:
        _run(channel.send_investigation_summary(_make_investigation()))
    fake_post.assert_not_called()


def test_channel_posts_when_webhook_configured():
    channel = SlackNotificationChannel(
        WebhookNotificationConfig(webhook_url="https://hooks.slack.com/services/T/B/xyz")
    )
    with patch("alerts.notifications.channels._post_slack") as fake_post:
        _run(channel.send_investigation_summary(_make_investigation()))
    fake_post.assert_called_once()
    url, payload = fake_post.call_args.args
    assert url == "https://hooks.slack.com/services/T/B/xyz"
    assert payload["blocks"][0]["type"] == "header"


def test_channel_swallows_transport_errors():
    """The never-raises contract — Slack outages must not fail investigations."""
    channel = SlackNotificationChannel(
        WebhookNotificationConfig(webhook_url="https://hooks.slack.com/services/T/B/xyz")
    )
    with patch(
        "alerts.notifications.channels._post_slack",
        side_effect=RuntimeError("slack is down"),
    ):
        _run(channel.send_investigation_summary(_make_investigation()))
    # If we got here without an exception, the never-raises contract holds.


def test_channel_strips_whitespace_webhook_url():
    """Whitespace-only webhook_url is treated as unset."""
    channel = SlackNotificationChannel(WebhookNotificationConfig(webhook_url="   "))
    with patch("alerts.notifications.channels._post_slack") as fake_post:
        _run(channel.send_investigation_summary(_make_investigation()))
    fake_post.assert_not_called()


def test_channel_offloads_slack_post_to_thread():
    """The Slack POST is blocking urllib — it MUST NOT run on the event loop.

    A hung Slack webhook would otherwise stall the alerts dispatcher for up
    to _SLACK_TIMEOUT_SECS (8s) per channel iteration.
    """
    channel = SlackNotificationChannel(
        WebhookNotificationConfig(webhook_url="https://hooks.slack.com/services/T/B/xyz")
    )
    with patch(
        "alerts.notifications.channels.asyncio.to_thread",
        wraps=asyncio.to_thread,
    ) as fake_to_thread:
        with patch("alerts.notifications.channels._post_slack") as fake_post:
            _run(channel.send_investigation_summary(_make_investigation()))
    fake_to_thread.assert_called_once()
    # First positional arg must be the sync _post_slack function.
    assert fake_to_thread.call_args.args[0] is fake_post
