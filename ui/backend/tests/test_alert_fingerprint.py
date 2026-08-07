"""What makes two deliveries "the same alert".

Dedup and resolved-matching both key off `Alert.fingerprint`, so this decides
whether either works at all — and it fails silently in both directions. Too
volatile and every delivery looks new; too loose and unrelated alerts collapse
into one investigation.

The bug this file was written for: annotations were part of the hash.
Alertmanager annotations are templated — `"CPU is {{ $value }}%"` renders as
"CPU is 93%", then "CPU is 94%" on the next delivery. So the same ongoing alert
produced a different fingerprint every time it was re-sent, and dedup never
fired on any Alertmanager with a templated description. Which is the default.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[3] / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from alerts.domain.alert import Alert  # noqa: E402
from alerts.domain.enums import AlertSource  # noqa: E402
from alerts.domain.normalization import normalize_alert_payload  # noqa: E402

STARTED = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def _alert(**overrides) -> Alert:
    base = dict(
        source=AlertSource.ALERTMANAGER,
        name="HighCPU",
        severity="critical",
        labels={"namespace": "demo", "pod": "api-0"},
        annotations={"description": "CPU is 93%"},
        starts_at=STARTED,
    )
    base.update(overrides)
    return Alert.from_parts(**base)


# ── what must NOT change the fingerprint ──────────────────────────────────


def test_a_templated_annotation_value_does_not_change_it():
    """The regression this exists for. Every re-send of a `$value` alert
    carries different text; hashing it made dedup a no-op in the common case."""
    first = _alert(annotations={"description": "CPU is 93%"})
    second = _alert(annotations={"description": "CPU is 94%"})

    assert first.fingerprint == second.fingerprint


def test_adding_an_annotation_does_not_change_it():
    assert _alert().fingerprint == _alert(
        annotations={"description": "CPU is 93%", "runbook_url": "https://…"}
    ).fingerprint


def test_status_does_not_change_it():
    """Resolved-matching depends on this: the resolved delivery has to hash to
    the same value as the firing one, or nothing is ever closed."""
    assert _alert(status="firing").fingerprint == _alert(status="resolved").fingerprint


def test_ends_at_does_not_change_it():
    """Only the resolved delivery carries endsAt."""
    assert _alert().fingerprint == _alert(ends_at=STARTED + timedelta(hours=1)).fingerprint


# ── what MUST change it ───────────────────────────────────────────────────


def test_a_different_label_set_is_a_different_alert():
    """Labels are the identity. Same alert name on a different pod is a
    different problem and deserves its own investigation."""
    assert _alert().fingerprint != _alert(
        labels={"namespace": "demo", "pod": "api-1"}
    ).fingerprint


def test_a_different_alert_name_is_a_different_alert():
    assert _alert().fingerprint != _alert(name="DiskFull").fingerprint


def test_a_later_firing_episode_is_a_different_alert():
    """startsAt is stable across re-sends within one episode and moves when the
    alert stops and starts again — so the fallback distinguishes episodes."""
    assert _alert().fingerprint != _alert(starts_at=STARTED + timedelta(days=1)).fingerprint


def test_the_same_alert_from_a_different_source_is_not_conflated():
    assert _alert().fingerprint != _alert(source=AlertSource.GRAFANA).fingerprint


# ── preferring Alertmanager's own fingerprint ─────────────────────────────


def test_alertmanagers_fingerprint_is_used_when_it_sends_one():
    """It computes one from the label set and repeats it on every delivery of
    the same alert. Using it means dedup agrees with the system that decided to
    re-send."""
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "b5f7c0de1a2b3c4d",
                "startsAt": "2026-08-06T12:00:00Z",
                "labels": {"alertname": "HighCPU", "namespace": "demo"},
                "annotations": {"description": "CPU is 93%"},
            }
        ],
    }

    assert normalize_alert_payload(payload)[0].fingerprint == "b5f7c0de1a2b3c4d"


def test_a_firing_and_resolved_pair_match_through_normalization():
    """The end-to-end property everything else rests on, exercised the way the
    webhook actually receives it — including Alertmanager's habit of sending
    different annotation text and an endsAt on the resolved delivery."""
    def payload(status: str) -> dict:
        alert = {
            "status": status,
            "fingerprint": "b5f7c0de1a2b3c4d",
            "startsAt": "2026-08-06T12:00:00Z",
            "labels": {"alertname": "HighCPU", "namespace": "demo"},
            "annotations": {"description": f"CPU is {'93' if status == 'firing' else '11'}%"},
        }
        if status == "resolved":
            alert["endsAt"] = "2026-08-06T12:30:00Z"
        return {"status": status, "alerts": [alert]}

    firing = normalize_alert_payload(payload("firing"))[0]
    resolved = normalize_alert_payload(payload("resolved"))[0]

    assert firing.fingerprint == resolved.fingerprint


def test_an_absent_fingerprint_falls_back_to_the_computed_one():
    """Grafana and Loki do not send one, and neither do older Alertmanagers."""
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "startsAt": "2026-08-06T12:00:00Z",
                "labels": {"alertname": "HighCPU", "namespace": "demo"},
                "annotations": {"description": "CPU is 93%"},
            }
        ],
    }

    fingerprint = normalize_alert_payload(payload)[0].fingerprint

    assert fingerprint
    assert len(fingerprint) == 64  # sha256 hex — ours, not Alertmanager's


def test_an_empty_fingerprint_field_falls_back_rather_than_collapsing():
    """An empty string would make every such alert dedup into one
    investigation — the worst possible failure, and a silent one."""
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "",
                "startsAt": "2026-08-06T12:00:00Z",
                "labels": {"alertname": "HighCPU", "namespace": "demo"},
                "annotations": {},
            }
        ],
    }

    assert len(normalize_alert_payload(payload)[0].fingerprint) == 64


def test_two_alerts_in_one_batch_keep_their_own_fingerprints():
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "aaaa1111",
                "labels": {"alertname": "HighCPU"},
                "annotations": {},
            },
            {
                "status": "firing",
                "fingerprint": "bbbb2222",
                "labels": {"alertname": "DiskFull"},
                "annotations": {},
            },
        ],
    }

    assert [a.fingerprint for a in normalize_alert_payload(payload)] == [
        "aaaa1111",
        "bbbb2222",
    ]
