"""Alertmanager polling for desktop notifications.

Server mode receives alerts by webhook. A laptop has no address Alertmanager
can post to, so desktop polls instead — and everything here is about not
being obnoxious while doing it.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for candidate in (BACKEND_DIR, MCP_DIR):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import desktop_alerts  # noqa: E402
import desktop_config  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point config.json at a temp dir so tests never touch real settings."""
    monkeypatch.setattr(
        desktop_config.desktop_paths, "config_path", lambda: tmp_path / "config.json"
    )
    return tmp_path


def alert(name="HighMemory", namespace="prod", pod="api-0", state="active", **labels):
    return {
        "labels": {"alertname": name, "namespace": namespace, "pod": pod,
                   "severity": "critical", **labels},
        "annotations": {"summary": f"{name} on {pod}"},
        "startsAt": datetime.now(UTC).isoformat(),
        "status": {"state": state},
        "generatorURL": "http://prometheus/graph",
    }


# ── the v2 / webhook format gap ───────────────────────────────────────────


def test_v2_status_object_becomes_a_string():
    """GET /api/v2/alerts reports status as an object; a webhook sends a
    string. Unadapted, every alert normalizes with a stringified dict."""
    shaped = desktop_alerts._v2_to_webhook_shape([alert(state="active")])
    assert shaped["alerts"][0]["status"] == "firing"

    shaped = desktop_alerts._v2_to_webhook_shape([alert(state="resolved")])
    assert shaped["alerts"][0]["status"] == "resolved"


def test_v2_adapter_tolerates_junk():
    shaped = desktop_alerts._v2_to_webhook_shape(
        ["not-a-dict", {"labels": {}}, {"labels": {}, "status": "firing"}]
    )
    assert len(shaped["alerts"]) == 2
    assert all(isinstance(a["status"], str) for a in shaped["alerts"])


# ── not being obnoxious ───────────────────────────────────────────────────


def test_first_poll_announces_nothing(store):
    """Opening the laptop on Monday must not fire a notification for every
    alert that has been firing all weekend."""
    poller = desktop_alerts.AlertPoller()
    fresh = poller.ingest([alert(name="A"), alert(name="B", pod="api-1")])
    assert fresh == []
    assert poller.drain() == []


def test_second_poll_announces_only_what_is_new(store):
    poller = desktop_alerts.AlertPoller()
    existing = alert(name="A")
    poller.ingest([existing])                      # primes

    fresh = poller.ingest([existing, alert(name="B", pod="api-1")])
    assert [a["name"] for a in fresh] == ["B"]


def test_an_alert_is_announced_once(store):
    poller = desktop_alerts.AlertPoller()
    poller.ingest([])
    new = alert(name="Flapping")
    assert len(poller.ingest([new])) == 1
    assert poller.ingest([new]) == []
    assert poller.ingest([new]) == []


def test_resolved_alerts_are_not_announced(store):
    poller = desktop_alerts.AlertPoller()
    poller.ingest([])
    assert poller.ingest([alert(name="Gone", state="resolved")]) == []


def test_queue_is_bounded(store):
    """A flapping cluster must not grow the queue without bound while the
    shell is not draining it."""
    poller = desktop_alerts.AlertPoller()
    poller.ingest([])
    poller.ingest([alert(name=f"A{i}", pod=f"pod-{i}") for i in range(200)])
    assert len(poller.drain()) == desktop_alerts.MAX_QUEUED


def test_drain_is_destructive(store):
    """Re-delivering would mean duplicate OS notifications for one alert."""
    poller = desktop_alerts.AlertPoller()
    poller.ingest([])
    poller.ingest([alert(name="Once")])
    assert len(poller.drain()) == 1
    assert poller.drain() == []


def test_summary_carries_what_a_click_needs(store):
    poller = desktop_alerts.AlertPoller()
    poller.ingest([])
    poller.ingest([alert(name="OOMKilled", namespace="payments", pod="checkout-7")])
    item = poller.drain()[0]
    assert item["name"] == "OOMKilled"
    assert item["namespace"] == "payments"
    assert item["pod"] == "checkout-7"
    assert item["severity"] == "critical"
    assert item["summary"]
    assert item["fingerprint"]


# ── configuration ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://alertmanager:9093", "http://alertmanager:9093"),
        ("https://am.example.com/", "https://am.example.com"),
        ("localhost:9093", "http://localhost:9093"),   # what a port-forward gives you
        ("  http://a:9093  ", "http://a:9093"),
        # Behind an ingress the path matters and must survive.
        ("https://ops.example.com/alertmanager/", "https://ops.example.com/alertmanager"),
        ("", ""),
    ],
)
def test_url_normalization(raw, expected):
    assert desktop_config.normalize_alertmanager_url(raw) == expected


# "http://" once became "http://http:" — trailing slashes were stripped before
# the scheme check, so the bare scheme had a second one prepended and passed.
@pytest.mark.parametrize("raw", ["ftp://host", "://nohost", "http://", "https:///"])
def test_url_rejection(raw):
    with pytest.raises(ValueError):
        desktop_config.normalize_alertmanager_url(raw)


def test_config_round_trips(store):
    desktop_config.save({"alertmanager_url": "http://a:9093", "notifications_enabled": True})
    loaded = desktop_config.load()
    assert loaded["alertmanager_url"] == "http://a:9093"
    assert loaded["notifications_enabled"] is True


def test_config_drops_unknown_keys(store):
    desktop_config.save({"alertmanager_url": "http://a:9093", "sneaky": "value"})
    assert "sneaky" not in json.loads((store / "config.json").read_text())


def test_config_is_owner_only(store):
    """It can carry basic-auth credentials in the userinfo portion."""
    desktop_config.save({"alertmanager_url": "http://user:pass@a:9093"})
    assert oct((store / "config.json").stat().st_mode & 0o777) == "0o600"


def test_corrupt_config_falls_back_to_defaults(store):
    """A window that will not open cannot be used to fix a broken config."""
    (store / "config.json").write_text("{not json")
    assert desktop_config.load() == desktop_config.DEFAULTS


# ── waking the poll loop ──────────────────────────────────────────────────


def test_refresh_cuts_the_sleep_short(store):
    """Enabling notifications must prime against the cluster *now*.

    Without this the first poll lands up to a full interval later, and the
    priming pass writes off anything that started firing in that gap as
    pre-existing — so the first real alert after switching the feature on is
    silently swallowed. Observed end-to-end before it was fixed.
    """
    poller = desktop_alerts.AlertPoller()
    assert not poller._wake.is_set()
    poller.refresh()
    assert poller._wake.is_set(), "refresh() must interrupt the inter-poll wait"


def test_stop_also_wakes_the_loop(store):
    """Otherwise shutdown waits out a full interval."""
    poller = desktop_alerts.AlertPoller()
    poller.stop()
    assert poller._stop.is_set()
    assert poller._wake.is_set()


def test_disabling_forgets_the_primer(store):
    """Turning notifications off and on again must not replay the backlog."""
    poller = desktop_alerts.AlertPoller()
    poller.ingest([alert(name="A")])
    assert poller._primed is True
    poller._primed = False          # what the disabled branch of _run does
    assert poller.ingest([alert(name="B", pod="api-9")]) == []
