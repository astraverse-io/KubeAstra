"""Resolved handling — the other half of the alert lifecycle.

Dedup made repeat *firing* deliveries reuse one investigation. Nothing closed
it. A `resolved` delivery fell through the same path as a firing one and
started a brand new investigation into a condition that had already stopped:
an LLM run, a row, and a notification, for a problem that was over.

Two things make this work at all, and both are quiet if wrong:

  * `status` is not part of `Alert.fingerprint`, so the resolved delivery
    hashes to the same value as the firing one. If it were included, nothing
    would ever match and resolution would silently never happen.
  * `resolved` has to be terminal. If it is left out of the terminal set, a
    closed investigation keeps absorbing repeats and the alert stops producing
    anything at all.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import db  # noqa: E402


@pytest.fixture
def clean_db():
    db.init_db()
    with db._conn() as con:
        con.execute("DELETE FROM investigations")
    yield db
    with db._conn() as con:
        con.execute("DELETE FROM investigations")


def _insert(fingerprint: str, status: str = "received", minutes_ago: int = 0) -> str:
    investigation_id = str(uuid.uuid4())
    created = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    with db._conn() as con:
        con.execute(
            "INSERT INTO investigations "
            "(id, namespace, severity, source, status, created_at, document, "
            " fingerprint, occurrence_count, last_seen_at) "
            "VALUES (?, 'demo', 'critical', 'test', ?, ?, '{}', ?, 1, ?)",
            (investigation_id, status, created, fingerprint, created),
        )
    return investigation_id


def _row(investigation_id: str):
    with db._conn() as con:
        return con.execute(
            "SELECT status, resolved_at, mttr_seconds, created_at "
            "FROM investigations WHERE id = ?",
            (investigation_id,),
        ).fetchone()


# ── the fingerprint has to survive the status change ──────────────────────


def test_firing_and_resolved_hash_to_the_same_fingerprint():
    """The whole mechanism rests on this. If `status` ever enters the
    fingerprint, resolution stops matching and nothing reports it."""
    from alerts.domain.alert import Alert

    started = datetime.now(timezone.utc)
    common = dict(
        name="HighCPU",
        source="alertmanager",
        severity="critical",
        labels={"namespace": "demo"},
        starts_at=started,
    )

    firing = Alert.from_parts(status="firing", **common)
    resolved = Alert.from_parts(status="resolved", **common)

    assert firing.fingerprint == resolved.fingerprint


# ── closing an investigation ──────────────────────────────────────────────


def test_resolving_marks_it_terminal_and_records_a_recovery_time(clean_db):
    investigation_id = _insert("fp-abc", minutes_ago=30)

    seconds = db.resolve_investigation(investigation_id)

    assert seconds == pytest.approx(30 * 60, abs=5)
    row = _row(investigation_id)
    assert row["status"] == "resolved"
    assert row["resolved_at"] > row["created_at"]
    assert row["mttr_seconds"] == pytest.approx(30 * 60, abs=5)


@pytest.mark.parametrize("status", ["received", "classified", "running"])
def test_any_open_status_can_be_resolved(clean_db, status):
    """An alert can stop firing while the investigation is still running — that
    is the common case for a transient problem, not an edge case."""
    investigation_id = _insert("fp-abc", status=status)

    assert db.resolve_investigation(investigation_id) is not None
    assert _row(investigation_id)["status"] == "resolved"


def test_a_resolved_investigation_no_longer_absorbs_repeats(clean_db):
    """If `resolved` were not terminal, the alert would go quiet forever: every
    later firing delivery would dedup into the closed investigation."""
    investigation_id = _insert("fp-abc")
    db.resolve_investigation(investigation_id)

    assert db.find_open_investigation("fp-abc") is None


def test_a_second_resolved_delivery_does_not_move_the_clock(clean_db):
    """Alertmanager re-sends resolved notifications. Without the status guard,
    the second one would recompute the recovery time from creation to *now* and
    inflate it."""
    investigation_id = _insert("fp-abc", minutes_ago=10)
    first = db.resolve_investigation(investigation_id)

    assert db.resolve_investigation(investigation_id) is None
    assert _row(investigation_id)["mttr_seconds"] == pytest.approx(first, abs=1)


def test_resolving_something_already_finished_is_not_an_error(clean_db):
    """A completed investigation stays completed — it produced an answer, and
    overwriting that with `resolved` would lose it."""
    investigation_id = _insert("fp-abc", status="completed")

    assert db.resolve_investigation(investigation_id) is None
    assert _row(investigation_id)["status"] == "completed"


def test_resolving_an_unknown_id_is_not_an_error(clean_db):
    assert db.resolve_investigation("no-such-id") is None


def test_a_skewed_clock_cannot_report_negative_recovery(clean_db):
    """A row written by a host running ahead would otherwise produce a negative
    time to recovery, which is worse than a zero — it poisons any average."""
    investigation_id = _insert("fp-abc", minutes_ago=-60)

    assert db.resolve_investigation(investigation_id) == 0.0


# ── the webhook path ──────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch, clean_db):
    from fastapi.testclient import TestClient

    from main import app
    from routers import alerts as alerts_router

    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_ENABLED", "true")
    monkeypatch.delenv("ALERT_WEBHOOK_TOKEN", raising=False)
    alerts_router.reset_webhook_settings()
    monkeypatch.setattr(
        alerts_router, "orchestrate_investigation", lambda *a, **k: None
    )
    return TestClient(app)


def _payload(status: str) -> dict:
    # startsAt is fixed so the firing and resolved deliveries describe the same
    # alert instance, exactly as Alertmanager sends them.
    return {
        "status": status,
        "alerts": [
            {
                "status": status,
                "startsAt": "2026-08-06T12:00:00Z",
                "labels": {
                    "alertname": "HighCPU",
                    "severity": "critical",
                    "namespace": "demo",
                },
                "annotations": {"description": "CPU is high"},
            }
        ],
    }


def _count() -> int:
    with db._conn() as con:
        return con.execute("SELECT COUNT(*) c FROM investigations").fetchone()["c"]


def test_a_resolved_delivery_closes_the_investigation_it_opened(client):
    firing = client.post("/api/v1/alerts/webhook", json=_payload("firing")).json()
    resolved = client.post("/api/v1/alerts/webhook", json=_payload("resolved")).json()

    assert resolved["investigation_ids"] == firing["investigation_ids"]
    assert resolved["resolved"] == 1
    assert _count() == 1
    assert _row(firing["investigation_ids"][0])["status"] == "resolved"


def test_a_resolved_delivery_never_starts_an_investigation(client):
    """The condition has already stopped. Investigating it burns an LLM run to
    describe something that is no longer happening."""
    response = client.post("/api/v1/alerts/webhook", json=_payload("resolved")).json()

    assert response["investigation_ids"] == []
    assert response["resolved"] == 0
    assert _count() == 0


def test_the_alert_can_fire_again_after_resolving(client):
    """A genuine re-occurrence deserves its own investigation — resolved is
    terminal, so the second firing must not dedup into the closed one."""
    client.post("/api/v1/alerts/webhook", json=_payload("firing"))
    client.post("/api/v1/alerts/webhook", json=_payload("resolved"))
    again = client.post("/api/v1/alerts/webhook", json=_payload("firing")).json()

    assert again["deduplicated"] == 0
    assert _count() == 2


def test_a_mixed_batch_is_counted_correctly(client):
    """Alertmanager batches firing and resolved alerts into one POST."""
    client.post("/api/v1/alerts/webhook", json=_payload("firing"))

    batch = {
        "status": "firing",
        "alerts": [
            _payload("resolved")["alerts"][0],
            {
                "status": "firing",
                "startsAt": "2026-08-06T12:30:00Z",
                "labels": {
                    "alertname": "DiskFull",
                    "severity": "warning",
                    "namespace": "demo",
                },
                "annotations": {"description": "disk"},
            },
        ],
    }
    response = client.post("/api/v1/alerts/webhook", json=batch).json()

    assert response["resolved"] == 1
    assert response["deduplicated"] == 0
    assert len(response["investigation_ids"]) == 2
    assert _count() == 2


def test_recovery_time_is_persisted_through_the_webhook(client):
    """The number is only useful if it survives the request — it is what
    "how long was this actually broken" reads from."""
    firing = client.post("/api/v1/alerts/webhook", json=_payload("firing")).json()
    client.post("/api/v1/alerts/webhook", json=_payload("resolved"))

    row = _row(firing["investigation_ids"][0])
    assert row["mttr_seconds"] is not None
    assert row["mttr_seconds"] >= 0
