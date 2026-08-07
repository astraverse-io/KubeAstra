"""Silences — stop investigating a condition somebody already understands.

A bad rollout being rolled back fires the same alert for every affected pod,
every repeat interval, and each one was an LLM-backed investigation into a
cause the operator already knew. Dedup collapses repeats of *one* alert; it
does nothing for a hundred pods each firing their own.

The failure modes here are asymmetric and worth naming. A silence that matches
too little is noise. A silence that matches too much is a cluster nobody is
watching — and it does not announce itself, because the absence of alerts looks
exactly like everything being fine.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import alert_silences  # noqa: E402
import db  # noqa: E402


@pytest.fixture
def clean_db():
    db.init_db()
    with db._conn() as con:
        con.execute("DELETE FROM alert_silences")
        con.execute("DELETE FROM investigations")
    yield db
    with db._conn() as con:
        con.execute("DELETE FROM alert_silences")
        con.execute("DELETE FROM investigations")


def _silence(matchers: list[dict]) -> dict:
    return {"matchers": matchers}


# ── matcher semantics ─────────────────────────────────────────────────────


def test_equality_matches_and_a_different_value_does_not():
    silence = _silence([{"label": "namespace", "op": "=", "value": "payments"}])

    assert alert_silences.matches(silence, {"namespace": "payments"})
    assert not alert_silences.matches(silence, {"namespace": "checkout"})


def test_every_matcher_must_match():
    """AND semantics. If any one matcher could carry the silence alone, a
    narrowing clause would widen it instead."""
    silence = _silence([
        {"label": "namespace", "op": "=", "value": "payments"},
        {"label": "severity", "op": "=", "value": "warning"},
    ])

    assert alert_silences.matches(
        silence, {"namespace": "payments", "severity": "warning"}
    )
    assert not alert_silences.matches(
        silence, {"namespace": "payments", "severity": "critical"}
    )


def test_a_regex_matcher_is_anchored():
    """`api-.*` must not silence `legacy-api-7`. An unanchored regex is the
    usual way a silence quietly covers more than intended."""
    silence = _silence([{"label": "pod", "op": "=~", "value": "api-.*"}])

    assert alert_silences.matches(silence, {"pod": "api-7d4f9b"})
    assert not alert_silences.matches(silence, {"pod": "legacy-api-7"})


def test_negated_operators_invert():
    assert alert_silences.matches(
        _silence([{"label": "severity", "op": "!=", "value": "critical"}]),
        {"severity": "warning"},
    )
    assert not alert_silences.matches(
        _silence([{"label": "pod", "op": "!~", "value": "api-.*"}]),
        {"pod": "api-1"},
    )


def test_a_missing_label_reads_as_empty():
    """Matches Alertmanager: `severity != critical` should hold for an alert
    carrying no severity at all."""
    labels = {"namespace": "payments"}

    assert not alert_silences.matches(
        _silence([{"label": "severity", "op": "=", "value": "critical"}]), labels
    )
    assert alert_silences.matches(
        _silence([{"label": "severity", "op": "!=", "value": "critical"}]), labels
    )


def test_an_unparsable_stored_silence_matches_nothing(clean_db):
    """`matchers` is None when the stored JSON did not parse. Under AND
    semantics an empty matcher list matches *everything*, so this has to fail
    closed — the alternative is one corrupt row silencing the cluster."""
    assert not alert_silences.matches({"matchers": None}, {"namespace": "payments"})
    assert not alert_silences.matches({"matchers": []}, {"namespace": "payments"})


# ── what may be stored ────────────────────────────────────────────────────


def test_an_empty_matcher_list_is_refused():
    """The most damaging mistake available here, and it looks like an empty
    form: no matchers means every alert matches."""
    with pytest.raises(alert_silences.InvalidMatcher):
        alert_silences.validate_matchers([])


def test_an_invalid_regex_is_refused_at_create_time():
    """Deferring this to ingest means a silence that raises on every incoming
    alert, taking ingestion down with it."""
    with pytest.raises(alert_silences.InvalidMatcher):
        alert_silences.validate_matchers(
            [{"label": "pod", "op": "=~", "value": "api-(["}]
        )


def test_an_unknown_operator_is_refused():
    with pytest.raises(alert_silences.InvalidMatcher):
        alert_silences.validate_matchers(
            [{"label": "pod", "op": "contains", "value": "api"}]
        )


def test_a_matcher_without_a_label_is_refused():
    with pytest.raises(alert_silences.InvalidMatcher):
        alert_silences.validate_matchers([{"op": "=", "value": "payments"}])


def test_an_enormous_regex_is_refused():
    """Operator-supplied, then evaluated against every incoming alert — a
    catastrophically backtracking pattern would be self-inflicted DoS on the
    ingest path."""
    with pytest.raises(alert_silences.InvalidMatcher):
        alert_silences.validate_matchers(
            [{"label": "pod", "op": "=~", "value": "a" * 500}]
        )


def test_validation_normalises_and_defaults_the_operator():
    assert alert_silences.validate_matchers([{"label": " pod ", "value": "api-1"}]) == [
        {"label": "pod", "op": "=", "value": "api-1"}
    ]


# ── storage and lifetime ──────────────────────────────────────────────────


def test_an_active_silence_is_listed(clean_db):
    db.create_silence("s1", [{"label": "ns", "op": "=", "value": "a"}], "why", "me", 3600)

    listed = db.list_active_silences()

    assert [s["id"] for s in listed] == ["s1"]
    assert listed[0]["matchers"] == [{"label": "ns", "op": "=", "value": "a"}]


def test_an_expired_silence_stops_applying_without_a_sweeper(clean_db):
    """Expiry is evaluated in the query, so a silence lapses on time even if
    nothing has run since it was created."""
    db.create_silence("s1", [{"label": "ns", "op": "=", "value": "a"}], "why", "me", -1)

    assert db.list_active_silences() == []
    assert len(db.list_all_silences()) == 1


def test_revoking_ends_it_immediately(clean_db):
    db.create_silence("s1", [{"label": "ns", "op": "=", "value": "a"}], "why", "me", 3600)

    assert db.revoke_silence("s1") is True
    assert db.list_active_silences() == []


def test_revoking_twice_reports_that_it_was_already_over(clean_db):
    db.create_silence("s1", [{"label": "ns", "op": "=", "value": "a"}], "why", "me", 3600)
    db.revoke_silence("s1")

    assert db.revoke_silence("s1") is False


def test_a_revoked_silence_is_kept_not_deleted(clean_db):
    """"Who silenced this, and for how long" is the first question asked after
    an alert nobody saw."""
    db.create_silence("s1", [{"label": "ns", "op": "=", "value": "a"}], "rollback", "sre@x", 3600)
    db.revoke_silence("s1")

    stored = db.get_silence("s1")
    assert stored["created_by"] == "sre@x"
    assert stored["reason"] == "rollback"
    assert stored["revoked_at"] is not None


def test_matches_are_counted(clean_db):
    """A silence nobody can see working is one nobody trusts — and a count far
    above expectations is the first sign its matchers are too broad."""
    db.create_silence("s1", [{"label": "ns", "op": "=", "value": "a"}], "why", "me", 3600)

    for _ in range(3):
        db.record_silence_match("s1")

    assert db.get_silence("s1")["matched_count"] == 3


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


def _payload(status: str = "firing", namespace: str = "payments") -> dict:
    return {
        "status": status,
        "alerts": [
            {
                "status": status,
                "fingerprint": f"fp-{namespace}",
                "startsAt": "2026-08-06T12:00:00Z",
                "labels": {
                    "alertname": "HighCPU",
                    "severity": "critical",
                    "namespace": namespace,
                },
                "annotations": {"description": "CPU is high"},
            }
        ],
    }


def _investigations() -> int:
    with db._conn() as con:
        return con.execute("SELECT COUNT(*) c FROM investigations").fetchone()["c"]


def test_a_silenced_alert_starts_no_investigation(client):
    db.create_silence(
        "s1", [{"label": "namespace", "op": "=", "value": "payments"}], "rollback", "me", 3600
    )

    response = client.post("/api/v1/alerts/webhook", json=_payload()).json()

    assert response["silenced"] == 1
    assert response["investigation_ids"] == []
    assert _investigations() == 0


def test_a_silence_only_covers_what_it_matches(client):
    db.create_silence(
        "s1", [{"label": "namespace", "op": "=", "value": "payments"}], "rollback", "me", 3600
    )

    response = client.post(
        "/api/v1/alerts/webhook", json=_payload(namespace="checkout")
    ).json()

    assert response["silenced"] == 0
    assert _investigations() == 1


def test_a_silenced_alert_still_counts_against_the_silence(client):
    db.create_silence(
        "s1", [{"label": "namespace", "op": "=", "value": "payments"}], "rollback", "me", 3600
    )

    client.post("/api/v1/alerts/webhook", json=_payload())
    client.post("/api/v1/alerts/webhook", json=_payload())

    assert db.get_silence("s1")["matched_count"] == 2


def test_an_expired_silence_lets_the_alert_through(client):
    db.create_silence(
        "s1", [{"label": "namespace", "op": "=", "value": "payments"}], "rollback", "me", -1
    )

    response = client.post("/api/v1/alerts/webhook", json=_payload()).json()

    assert response["silenced"] == 0
    assert _investigations() == 1


def test_silencing_mid_incident_does_not_strand_the_open_investigation(client):
    """Resolved handling runs before the silence gate for exactly this case.

    If the gate came first, an investigation opened before the silence would
    never be closed — and because it stayed open it would go on absorbing every
    later occurrence of that alert.
    """
    firing = client.post("/api/v1/alerts/webhook", json=_payload("firing")).json()
    db.create_silence(
        "s1", [{"label": "namespace", "op": "=", "value": "payments"}], "rollback", "me", 3600
    )

    resolved = client.post("/api/v1/alerts/webhook", json=_payload("resolved")).json()

    assert resolved["resolved"] == 1
    with db._conn() as con:
        row = con.execute(
            "SELECT status FROM investigations WHERE id = ?",
            (firing["investigation_ids"][0],),
        ).fetchone()
    assert row["status"] == "resolved"


# ── the API ───────────────────────────────────────────────────────────────


def test_creating_and_listing_a_silence(client):
    created = client.post(
        "/api/v1/alerts/silences",
        json={
            "matchers": [{"label": "namespace", "op": "=", "value": "payments"}],
            "reason": "rolling back a bad image",
            "ttl_seconds": 3600,
        },
    )

    assert created.status_code == 201
    assert created.json()["reason"] == "rolling back a bad image"

    listed = client.get("/api/v1/alerts/silences").json()
    assert listed["count"] == 1


def test_the_api_refuses_a_silence_with_no_matchers(client):
    response = client.post(
        "/api/v1/alerts/silences",
        json={"matchers": [], "reason": "oops", "ttl_seconds": 3600},
    )

    assert response.status_code == 400
    assert "every alert" in response.json()["detail"]


def test_the_api_refuses_an_invalid_regex(client):
    response = client.post(
        "/api/v1/alerts/silences",
        json={
            "matchers": [{"label": "pod", "op": "=~", "value": "api-(["}],
            "reason": "oops",
            "ttl_seconds": 3600,
        },
    )

    assert response.status_code == 400


def test_a_silence_cannot_be_created_without_an_expiry(client):
    """An unbounded silence is how a cluster goes unwatched for a month because
    somebody silenced an alert in April."""
    response = client.post(
        "/api/v1/alerts/silences",
        json={
            "matchers": [{"label": "namespace", "op": "=", "value": "payments"}],
            "reason": "forever please",
            "ttl_seconds": 0,
        },
    )

    assert response.status_code == 422


def test_a_silence_ttl_is_capped(client):
    response = client.post(
        "/api/v1/alerts/silences",
        json={
            "matchers": [{"label": "namespace", "op": "=", "value": "payments"}],
            "reason": "a year",
            "ttl_seconds": 365 * 24 * 3600,
        },
    )

    assert response.status_code == 422


def test_a_reason_is_required(client):
    response = client.post(
        "/api/v1/alerts/silences",
        json={
            "matchers": [{"label": "namespace", "op": "=", "value": "payments"}],
            "reason": "",
            "ttl_seconds": 3600,
        },
    )

    assert response.status_code == 422


def test_revoking_through_the_api(client):
    created = client.post(
        "/api/v1/alerts/silences",
        json={
            "matchers": [{"label": "namespace", "op": "=", "value": "payments"}],
            "reason": "rollback",
            "ttl_seconds": 3600,
        },
    ).json()

    assert client.delete(f"/api/v1/alerts/silences/{created['id']}").json()["revoked"]
    assert client.get("/api/v1/alerts/silences").json()["count"] == 0


def test_an_unknown_silence_is_a_404(client):
    assert client.get("/api/v1/alerts/silences/nope").status_code == 404
    assert client.delete("/api/v1/alerts/silences/nope").status_code == 404
