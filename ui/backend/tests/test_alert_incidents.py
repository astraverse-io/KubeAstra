"""Grouping alerts into incidents.

A crashlooping pod fires CrashLoopBackOff, then OOMKilled, then a failing
probe. Dedup does not help — those are three *different* alerts. Each got its
own investigation, its own LLM run, and produced its own answer that never
mentioned the other two.

The risk of grouping is the mirror image: merge too eagerly and a second, real
problem is buried inside the first incident and never investigated on its own
terms. So correlation only ever attaches; it never suppresses an investigation.
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
        con.execute("DELETE FROM incidents")
        con.execute("DELETE FROM alert_silences")
    yield db
    with db._conn() as con:
        con.execute("DELETE FROM investigations")
        con.execute("DELETE FROM incidents")
        con.execute("DELETE FROM alert_silences")


def _investigation(incident_id: str | None = None, status: str = "received") -> str:
    investigation_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with db._conn() as con:
        con.execute(
            "INSERT INTO investigations "
            "(id, namespace, severity, source, status, created_at, document, incident_id) "
            "VALUES (?, 'prod', 'critical', 'test', ?, ?, '{}', ?)",
            (investigation_id, status, now, incident_id),
        )
    return investigation_id


def _age_incident(incident_id: str, *, opened: int = 0, active: int = 0) -> None:
    """Move an incident's clocks back, in minutes."""
    now = datetime.now(timezone.utc)
    with db._conn() as con:
        con.execute(
            "UPDATE incidents SET opened_at = ?, last_active_at = ? WHERE id = ?",
            (
                (now - timedelta(minutes=opened)).isoformat(),
                (now - timedelta(minutes=active)).isoformat(),
                incident_id,
            ),
        )


OPEN = dict(window_minutes=10, max_lifetime_hours=24)


# ── opening and joining ───────────────────────────────────────────────────


def test_the_first_alert_opens_an_incident(clean_db):
    incident_id = db.find_or_open_incident("prod", "api", **OPEN)

    assert incident_id
    assert db.get_incident(incident_id)["alert_count"] == 1


def test_a_second_alert_for_the_same_workload_joins_it(clean_db):
    """CrashLoopBackOff then OOMKilled on one workload is one problem."""
    first = db.find_or_open_incident("prod", "api", **OPEN)
    second = db.find_or_open_incident("prod", "api", **OPEN)

    assert first == second
    assert db.get_incident(first)["alert_count"] == 2


def test_a_different_workload_gets_its_own_incident(clean_db):
    api = db.find_or_open_incident("prod", "api", **OPEN)
    worker = db.find_or_open_incident("prod", "worker", **OPEN)

    assert api != worker


def test_the_same_workload_in_another_namespace_is_separate(clean_db):
    """Attaching a staging incident to a production one would be worse than
    not grouping at all."""
    assert db.find_or_open_incident("prod", "api", **OPEN) != db.find_or_open_incident(
        "staging", "api", **OPEN
    )


def test_an_uncorrelatable_alert_opens_nothing(clean_db):
    """Empty key means "leave it alone" — the caller sets no incident_id rather
    than inventing a grouping."""
    assert db.find_or_open_incident("", "api", **OPEN) is None
    assert db.find_or_open_incident("prod", "", **OPEN) is None


# ── the window ────────────────────────────────────────────────────────────


def test_an_alert_after_the_window_starts_a_new_incident(clean_db):
    """A recurrence hours later is a new problem, even for the same workload."""
    first = db.find_or_open_incident("prod", "api", **OPEN)
    _age_incident(first, opened=60, active=60)

    assert db.find_or_open_incident("prod", "api", **OPEN) != first


def test_the_window_slides_on_activity_not_on_open_time(clean_db):
    """A workload firing continuously for an hour is one incident.

    Anchoring the window to the open time would start a fresh incident every
    ten minutes and reintroduce the fragmentation this removes.
    """
    first = db.find_or_open_incident("prod", "api", **OPEN)
    _age_incident(first, opened=55, active=1)

    assert db.find_or_open_incident("prod", "api", **OPEN) == first


def test_a_never_closing_incident_stops_absorbing_after_its_max_lifetime(clean_db):
    """Alertmanager can be configured with `send_resolved: false`, so nothing
    ever tells us the condition ended. Without this cap the incident would keep
    absorbing alerts forever and they would stop being investigated on their
    own terms."""
    first = db.find_or_open_incident("prod", "api", **OPEN)
    _age_incident(first, opened=60 * 48, active=1)

    assert db.find_or_open_incident("prod", "api", **OPEN) != first


# ── attaching and closing ─────────────────────────────────────────────────


def test_investigations_attach_to_the_incident(clean_db):
    incident_id = db.find_or_open_incident("prod", "api", **OPEN)
    first = _investigation()
    db.attach_to_incident(first, incident_id)

    assert db.get_incident(incident_id)["investigations"][0]["id"] == first


def test_an_incident_closes_once_every_investigation_is_terminal(clean_db):
    incident_id = db.find_or_open_incident("prod", "api", **OPEN)
    db.attach_to_incident(_investigation(status="completed"), incident_id)
    db.attach_to_incident(_investigation(status="resolved"), incident_id)

    assert db.close_incident_if_settled(incident_id) is True
    assert db.get_incident(incident_id)["closed_at"] is not None


def test_an_incident_stays_open_while_one_investigation_runs(clean_db):
    incident_id = db.find_or_open_incident("prod", "api", **OPEN)
    db.attach_to_incident(_investigation(status="completed"), incident_id)
    db.attach_to_incident(_investigation(status="running"), incident_id)

    assert db.close_incident_if_settled(incident_id) is False


def test_a_brand_new_incident_with_no_investigations_is_left_open(clean_db):
    """Closing it would immediately reopen a new one for the next alert of the
    same problem — the fragmentation this feature removes."""
    incident_id = db.find_or_open_incident("prod", "api", **OPEN)

    assert db.close_incident_if_settled(incident_id) is False


def test_closing_twice_reports_that_it_was_already_closed(clean_db):
    incident_id = db.find_or_open_incident("prod", "api", **OPEN)
    db.attach_to_incident(_investigation(status="completed"), incident_id)
    db.close_incident_if_settled(incident_id)

    assert db.close_incident_if_settled(incident_id) is False


def test_a_closed_incident_does_not_absorb_the_next_alert(clean_db):
    incident_id = db.find_or_open_incident("prod", "api", **OPEN)
    db.attach_to_incident(_investigation(status="completed"), incident_id)
    db.close_incident_if_settled(incident_id)

    assert db.find_or_open_incident("prod", "api", **OPEN) != incident_id


# ── listing ───────────────────────────────────────────────────────────────


def test_listing_shows_open_incidents_newest_activity_first(clean_db):
    older = db.find_or_open_incident("prod", "api", **OPEN)
    _age_incident(older, opened=5, active=5)
    newer = db.find_or_open_incident("prod", "worker", **OPEN)

    assert [i["id"] for i in db.list_incidents()] == [newer, older]


def test_closed_incidents_are_hidden_unless_asked_for(clean_db):
    incident_id = db.find_or_open_incident("prod", "api", **OPEN)
    db.attach_to_incident(_investigation(status="completed"), incident_id)
    db.close_incident_if_settled(incident_id)

    assert db.list_incidents() == []
    assert len(db.list_incidents(include_closed=True)) == 1


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


def _payload(alertname: str, pod: str = "api-7d4f9b8c5-x2k9p", status: str = "firing"):
    return {
        "status": status,
        "alerts": [
            {
                "status": status,
                "fingerprint": f"fp-{alertname}-{pod}",
                "startsAt": "2026-08-07T12:00:00Z",
                "labels": {
                    "alertname": alertname,
                    "severity": "critical",
                    "namespace": "prod",
                    "pod": pod,
                },
                "annotations": {"description": alertname},
            }
        ],
    }


def test_two_alerts_about_one_pod_share_an_incident(client):
    """The case this exists for: three symptoms of one crashloop."""
    client.post("/api/v1/alerts/webhook", json=_payload("CrashLoopBackOff"))
    client.post("/api/v1/alerts/webhook", json=_payload("OOMKilled"))

    incidents = client.get("/api/v1/alerts/incidents").json()

    assert incidents["count"] == 1
    assert len(incidents["incidents"][0]["investigation_ids"]) == 2


def test_both_alerts_are_still_investigated_separately(client):
    """Correlation groups; it must never suppress. A second symptom that turns
    out to be a second problem still gets its own investigation."""
    first = client.post("/api/v1/alerts/webhook", json=_payload("CrashLoopBackOff")).json()
    second = client.post("/api/v1/alerts/webhook", json=_payload("OOMKilled")).json()

    assert first["investigation_ids"] != second["investigation_ids"]
    assert second["correlated"] == 1
    with db._conn() as con:
        assert con.execute("SELECT COUNT(*) c FROM investigations").fetchone()["c"] == 2


def test_pods_of_the_same_deployment_correlate(client):
    """A bad rollout takes out several pods. One incident, not one per pod."""
    client.post("/api/v1/alerts/webhook", json=_payload("CrashLoopBackOff", pod="api-7d4f9b8c5-x2k9p"))
    client.post("/api/v1/alerts/webhook", json=_payload("CrashLoopBackOff", pod="api-7d4f9b8c5-qq7rt"))

    assert client.get("/api/v1/alerts/incidents").json()["count"] == 1


def test_a_different_workload_is_a_different_incident(client):
    client.post("/api/v1/alerts/webhook", json=_payload("CrashLoopBackOff", pod="api-7d4f9b8c5-x2k9p"))
    client.post("/api/v1/alerts/webhook", json=_payload("CrashLoopBackOff", pod="worker-6b2c8d4f7-mm3wz"))

    assert client.get("/api/v1/alerts/incidents").json()["count"] == 2


def test_an_alert_with_no_pod_or_workload_is_investigated_but_not_grouped(client):
    payload = _payload("NamespaceQuotaExceeded")
    del payload["alerts"][0]["labels"]["pod"]

    response = client.post("/api/v1/alerts/webhook", json=payload).json()

    assert response["correlated"] == 0
    assert len(response["investigation_ids"]) == 1
    assert client.get("/api/v1/alerts/incidents").json()["count"] == 0


def test_a_correlation_failure_does_not_stop_ingestion(client, monkeypatch):
    """Correlation is an enhancement. If it throws, the alert must still be
    investigated — a grouping bug should not take alerting down."""
    from routers import alerts as alerts_router

    def boom(*a, **k):
        raise RuntimeError("correlation exploded")

    monkeypatch.setattr(alerts_router.db, "find_or_open_incident", boom)

    response = client.post("/api/v1/alerts/webhook", json=_payload("CrashLoopBackOff")).json()

    assert len(response["investigation_ids"]) == 1
    assert response["correlated"] == 0


def test_the_incident_detail_endpoint_lists_its_investigations(client):
    client.post("/api/v1/alerts/webhook", json=_payload("CrashLoopBackOff"))
    incident_id = client.get("/api/v1/alerts/incidents").json()["incidents"][0]["id"]

    detail = client.get(f"/api/v1/alerts/incidents/{incident_id}").json()

    assert detail["workload"] == "api"
    assert detail["namespace"] == "prod"
    assert len(detail["investigations"]) == 1


def test_an_unknown_incident_is_a_404(client):
    assert client.get("/api/v1/alerts/incidents/nope").status_code == 404


def test_incidents_is_not_swallowed_by_the_alert_detail_route(client):
    """`/incidents` has to be declared before any `/{id}` route or it is read
    as an alert id and 404s."""
    assert client.get("/api/v1/alerts/incidents").status_code == 200
