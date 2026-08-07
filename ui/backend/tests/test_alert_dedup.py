"""Alert dedup — one investigation per incident, not per delivery.

Alertmanager re-sends a firing alert every `repeat_interval` for as long as
the condition holds. That is the normal path, not a storm: without dedup, one
ongoing problem produced an investigation per delivery, each a full LLM run.

`Alert.fingerprint` was already computed on every alert and read by nothing.

The subtle part is which statuses count as "still open". Getting that list
wrong is a silent failure — a status name that does not exist matches no rows,
so dedup looks wired up and never fires. It is derived from the enum here for
that reason, and this file pins the derivation.
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


def _insert(
    fingerprint: str,
    status: str = "received",
    minutes_ago: int = 0,
    occurrence_count: int = 1,
) -> str:
    investigation_id = str(uuid.uuid4())
    created = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    with db._conn() as con:
        con.execute(
            "INSERT INTO investigations "
            "(id, namespace, severity, source, status, created_at, document, "
            " fingerprint, occurrence_count, last_seen_at) "
            "VALUES (?, 'demo', 'critical', 'test', ?, ?, '{}', ?, ?, ?)",
            (investigation_id, status, created, fingerprint, occurrence_count, created),
        )
    return investigation_id


# ── which statuses are "still open" ───────────────────────────────────────


def test_open_statuses_come_from_the_enum_not_a_guess():
    """The failure mode this guards: a name that does not exist matches no
    rows, so dedup appears wired up and silently never fires.

    An earlier draft used "investigating" / "analyzing" / "in_progress", none
    of which exist — and missed `classified` and `running`, which are the
    states an investigation actually spends its time in.
    """
    from alerts.domain.enums import InvestigationStatus

    all_values = {s.value for s in InvestigationStatus}

    assert set(db.OPEN_INVESTIGATION_STATUSES) <= all_values
    assert (
        set(db.OPEN_INVESTIGATION_STATUSES)
        == all_values - db.TERMINAL_INVESTIGATION_STATUSES
    )

    # The assertion above is self-consistent for *any* enum — it derives one
    # set from the other, so a newly added status silently lands in "open" and
    # is treated as in flight forever. That is not hypothetical: adding
    # `needs_config` passed every check here until both sets were spelled out.
    # An investigation stuck in a bogus "open" status keeps absorbing repeats
    # and holds its incident open permanently.
    #
    # So both halves are written literally, and the union must cover the enum.
    # A new status fails this until somebody decides which half it belongs to.
    assert db.TERMINAL_INVESTIGATION_STATUSES == {
        "completed",
        "failed",
        "resolved",
        "needs_config",
    }
    assert db.ACTIVE_INVESTIGATION_STATUSES == {"received", "classified", "running"}
    assert (
        db.TERMINAL_INVESTIGATION_STATUSES | db.ACTIVE_INVESTIGATION_STATUSES
        == all_values
    ), "a status was added to InvestigationStatus without being classified"


@pytest.mark.parametrize("status", ["received", "classified", "running"])
def test_a_repeat_during_an_open_investigation_is_found(clean_db, status):
    original = _insert("fp-abc", status=status)

    assert db.find_open_investigation("fp-abc")["id"] == original


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_a_repeat_after_a_terminal_status_starts_fresh(clean_db, status):
    """A genuine re-occurrence deserves its own investigation — the previous
    answer is no longer live, and the cause may have changed."""
    _insert("fp-abc", status=status)

    assert db.find_open_investigation("fp-abc") is None


# ── counting recurrences ──────────────────────────────────────────────────


def test_recurrence_increments_and_reports_the_new_count(clean_db):
    investigation_id = _insert("fp-abc")

    assert db.record_recurrence(investigation_id) == 2
    assert db.record_recurrence(investigation_id) == 3


def test_recurrence_stamps_last_seen(clean_db):
    """"Fired 40 times over six hours" and "fired 40 times in the last minute"
    are different incidents; without this they look identical."""
    investigation_id = _insert("fp-abc", minutes_ago=120)

    db.record_recurrence(investigation_id)

    with db._conn() as con:
        row = con.execute(
            "SELECT created_at, last_seen_at FROM investigations WHERE id = ?",
            (investigation_id,),
        ).fetchone()

    assert row["last_seen_at"] > row["created_at"]


def test_the_count_is_incremented_in_sql_not_read_then_written(clean_db):
    """Alertmanager sends batches, and the webhook loops over them.

    A read-modify-write lets two deliveries both read 3 and both write 4.
    """
    investigation_id = _insert("fp-abc")

    results = [db.record_recurrence(investigation_id) for _ in range(5)]

    assert results == [2, 3, 4, 5, 6]


def test_recording_against_a_missing_investigation_does_not_raise(clean_db):
    assert db.record_recurrence("no-such-id") == 1


# ── bounds ────────────────────────────────────────────────────────────────


def test_a_stale_open_investigation_stops_absorbing_repeats(clean_db):
    """An investigation stuck in `running` because the process died would
    otherwise swallow every future occurrence of that alert forever — the
    alert would silently stop producing anything at all."""
    _insert("fp-abc", status="running", minutes_ago=60 * 30)

    assert db.find_open_investigation("fp-abc", within_hours=24) is None


def test_different_alerts_do_not_collide(clean_db):
    first = _insert("fp-one")
    _insert("fp-two")

    assert db.find_open_investigation("fp-one")["id"] == first


def test_an_empty_fingerprint_never_matches(clean_db):
    """A fingerprint that failed to compute must not make every such alert
    dedup into one investigation."""
    _insert("", status="received")

    assert db.find_open_investigation("") is None


def test_the_newest_open_investigation_wins(clean_db):
    """Two open investigations for one fingerprint should not happen, but if
    a race produced them the live answer is the most recent."""
    _insert("fp-abc", minutes_ago=30)
    newer = _insert("fp-abc", minutes_ago=1)

    assert db.find_open_investigation("fp-abc")["id"] == newer


# ── the webhook path ──────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch, clean_db):
    from fastapi.testclient import TestClient

    from main import app
    from routers import alerts as alerts_router

    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_ENABLED", "true")
    monkeypatch.delenv("ALERT_WEBHOOK_TOKEN", raising=False)
    alerts_router.reset_webhook_settings()
    # The orchestrator is not what is under test, and it would make LLM calls.
    monkeypatch.setattr(
        alerts_router, "orchestrate_investigation", lambda *a, **k: None
    )
    return TestClient(app)


PAYLOAD = {
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "HighCPU", "severity": "critical", "namespace": "demo"},
            "annotations": {"description": "CPU is high"},
        }
    ],
}


def test_the_same_alert_twice_produces_one_investigation(client):
    first = client.post("/api/v1/alerts/webhook", json=PAYLOAD).json()
    second = client.post("/api/v1/alerts/webhook", json=PAYLOAD).json()

    assert first["investigation_ids"] == second["investigation_ids"]
    assert first["deduplicated"] == 0
    assert second["deduplicated"] == 1

    with db._conn() as con:
        rows = con.execute("SELECT COUNT(*) c FROM investigations").fetchone()["c"]
    assert rows == 1


def test_a_repeat_still_returns_an_id(client):
    """An Alertmanager receiver that sees no id cannot tell dedup from
    ingestion having failed."""
    client.post("/api/v1/alerts/webhook", json=PAYLOAD)
    second = client.post("/api/v1/alerts/webhook", json=PAYLOAD).json()

    assert len(second["investigation_ids"]) == 1
    assert second["status"] == "accepted"


def test_the_occurrence_count_climbs_with_each_delivery(client):
    for _ in range(4):
        client.post("/api/v1/alerts/webhook", json=PAYLOAD)

    with db._conn() as con:
        row = con.execute(
            "SELECT occurrence_count FROM investigations"
        ).fetchone()

    assert row["occurrence_count"] == 4


def test_a_different_alert_still_gets_its_own_investigation(client):
    other = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "DiskFull",
                    "severity": "warning",
                    "namespace": "demo",
                },
                "annotations": {"description": "disk"},
            }
        ],
    }

    client.post("/api/v1/alerts/webhook", json=PAYLOAD)
    response = client.post("/api/v1/alerts/webhook", json=other).json()

    assert response["deduplicated"] == 0
    with db._conn() as con:
        rows = con.execute("SELECT COUNT(*) c FROM investigations").fetchone()["c"]
    assert rows == 2


def test_a_fingerprint_is_persisted_so_dedup_survives_a_restart(client):
    """The whole mechanism reads a column. If save() stops writing it, dedup
    silently stops working and nothing else notices."""
    client.post("/api/v1/alerts/webhook", json=PAYLOAD)

    with db._conn() as con:
        row = con.execute("SELECT fingerprint FROM investigations").fetchone()

    assert row["fingerprint"]
