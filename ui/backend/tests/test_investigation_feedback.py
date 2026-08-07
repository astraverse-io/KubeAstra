"""Was the investigation's answer any good?

Chat has had thumbs for a while, and a 👍 there promotes an answer into the
runbook collection. Alert-driven investigations produced root-cause answers
nobody could rate — so a playbook that was consistently wrong looked exactly
like one that always worked, and the only way to find out was for somebody to
remember.

What makes this actionable is not the score. It is the note beside a 👎: a rate
tells you *that* a playbook is failing, and only the text tells you how. That
is why notes are stored verbatim and travel with the counts instead of being
reduced to a number.
"""

from __future__ import annotations

import json
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


def _investigation(playbook: str = "crashloop", days_ago: int = 0) -> str:
    investigation_id = str(uuid.uuid4())
    created = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    document = json.dumps({"selected_playbook": playbook}) if playbook else "{}"
    with db._conn() as con:
        con.execute(
            "INSERT INTO investigations "
            "(id, namespace, severity, source, status, created_at, document) "
            "VALUES (?, 'prod', 'critical', 'test', 'completed', ?, ?)",
            (investigation_id, created, document),
        )
    return investigation_id


def _age_feedback(investigation_id: str, days_ago: int) -> None:
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with db._conn() as con:
        con.execute(
            "UPDATE investigations SET feedback_at = ? WHERE id = ?",
            (when, investigation_id),
        )


# ── recording a verdict ───────────────────────────────────────────────────


@pytest.mark.parametrize("rating", ["up", "down"])
def test_a_rating_is_recorded(clean_db, rating: str):
    investigation_id = _investigation()

    assert db.record_investigation_feedback(investigation_id, rating) is True

    with db._conn() as con:
        row = con.execute(
            "SELECT feedback_rating, feedback_at FROM investigations WHERE id = ?",
            (investigation_id,),
        ).fetchone()
    assert row["feedback_rating"] == rating
    assert row["feedback_at"]


def test_notes_are_stored_verbatim(clean_db):
    """Reducing them to a count would throw away the only part that says how to
    fix the playbook."""
    investigation_id = _investigation()
    db.record_investigation_feedback(
        investigation_id, "down", "blamed the probe; it was actually the PVC"
    )

    with db._conn() as con:
        row = con.execute(
            "SELECT feedback_notes FROM investigations WHERE id = ?",
            (investigation_id,),
        ).fetchone()
    assert row["feedback_notes"] == "blamed the probe; it was actually the PVC"


def test_a_rating_can_be_changed(clean_db):
    """Someone marks an answer wrong, then finds it was right after all. The
    summary should reflect the current view, not the first impression."""
    investigation_id = _investigation()
    db.record_investigation_feedback(investigation_id, "down", "wrong")
    db.record_investigation_feedback(investigation_id, "up")

    assert db.investigation_feedback_summary()[0]["up"] == 1
    assert db.investigation_feedback_summary()[0]["down"] == 0


def test_an_invalid_rating_is_refused(clean_db):
    """Anything outside the two values would land in the summary as its own
    silent bucket and skew every rate computed from it."""
    with pytest.raises(ValueError):
        db.record_investigation_feedback(_investigation(), "meh")


def test_rating_an_unknown_investigation_reports_failure(clean_db):
    assert db.record_investigation_feedback("no-such-id", "up") is False


def test_who_rated_it_is_recorded(clean_db):
    """"Who said this was wrong" is the first question when a playbook is about
    to be rewritten on the strength of it."""
    investigation_id = _investigation()
    db.record_investigation_feedback(investigation_id, "down", "wrong", "sre@example")

    with db._conn() as con:
        row = con.execute(
            "SELECT feedback_by FROM investigations WHERE id = ?",
            (investigation_id,),
        ).fetchone()
    assert row["feedback_by"] == "sre@example"


# ── the summary ───────────────────────────────────────────────────────────


def test_verdicts_are_grouped_by_playbook(clean_db):
    db.record_investigation_feedback(_investigation("crashloop"), "up")
    db.record_investigation_feedback(_investigation("crashloop"), "down", "no")
    db.record_investigation_feedback(_investigation("oom"), "up")

    summary = {e["playbook"]: e for e in db.investigation_feedback_summary()}

    assert summary["crashloop"]["up"] == 1
    assert summary["crashloop"]["down"] == 1
    assert summary["oom"]["up"] == 1


def test_the_worst_playbook_reads_first(clean_db):
    """The one most in need of editing should not have to be searched for."""
    db.record_investigation_feedback(_investigation("good"), "up")
    db.record_investigation_feedback(_investigation("good"), "up")
    db.record_investigation_feedback(_investigation("bad"), "down", "wrong")
    db.record_investigation_feedback(_investigation("bad"), "down", "wrong again")

    assert db.investigation_feedback_summary()[0]["playbook"] == "bad"


def test_volume_breaks_a_tie(clean_db):
    """Wrong twice out of two matters less than wrong thirty times out of
    thirty."""
    for _ in range(2):
        db.record_investigation_feedback(_investigation("rare"), "down", "x")
    for _ in range(30):
        db.record_investigation_feedback(_investigation("common"), "down", "x")

    assert db.investigation_feedback_summary()[0]["playbook"] == "common"


def test_the_down_rate_is_reported(clean_db):
    db.record_investigation_feedback(_investigation("p"), "up")
    db.record_investigation_feedback(_investigation("p"), "down", "no")
    db.record_investigation_feedback(_investigation("p"), "down", "no")

    assert db.investigation_feedback_summary()[0]["down_rate"] == pytest.approx(0.667, abs=0.01)


def test_sample_notes_travel_with_the_counts(clean_db):
    db.record_investigation_feedback(_investigation("p"), "down", "blamed the probe")

    assert db.investigation_feedback_summary()[0]["sample_notes"] == ["blamed the probe"]


def test_sample_notes_are_capped(clean_db):
    """A summary is for reading. Ten thousand notes inline is not a summary."""
    for i in range(20):
        db.record_investigation_feedback(_investigation("p"), "down", f"note {i}")

    assert len(db.investigation_feedback_summary()[0]["sample_notes"]) == 5


def test_unrated_investigations_are_not_counted(clean_db):
    """Silence is not approval, and counting it as such would make every
    playbook look good."""
    _investigation("p")
    db.record_investigation_feedback(_investigation("p"), "down", "no")

    assert db.investigation_feedback_summary()[0]["total"] == 1


def test_feedback_outside_the_window_is_excluded(clean_db):
    """A playbook edited last month should not be judged on the answers it gave
    before the edit."""
    old = _investigation("p")
    db.record_investigation_feedback(old, "down", "was broken")
    _age_feedback(old, days_ago=90)

    assert db.investigation_feedback_summary(within_days=30) == []
    assert db.investigation_feedback_summary(within_days=365)[0]["down"] == 1


def test_an_investigation_with_no_playbook_is_grouped_separately(clean_db):
    """A pile of unclassified failures is its own problem, and hiding them
    inside another playbook's numbers would misattribute it."""
    db.record_investigation_feedback(_investigation(playbook=""), "down", "no idea")

    assert db.investigation_feedback_summary()[0]["playbook"] == ""


def test_an_unparsable_document_does_not_break_the_summary(clean_db):
    """One malformed row must not cost every other playbook its numbers."""
    investigation_id = _investigation("p")
    with db._conn() as con:
        con.execute(
            "UPDATE investigations SET document = 'not json' WHERE id = ?",
            (investigation_id,),
        )
    db.record_investigation_feedback(investigation_id, "down", "x")
    db.record_investigation_feedback(_investigation("healthy"), "up")

    playbooks = {e["playbook"] for e in db.investigation_feedback_summary()}
    assert "healthy" in playbooks


# ── the API ───────────────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch, clean_db):
    from fastapi.testclient import TestClient

    from main import app
    from routers import alerts as alerts_router

    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_ENABLED", "true")
    alerts_router.reset_webhook_settings()
    return TestClient(app)


def test_submitting_feedback_through_the_api(client):
    investigation_id = _investigation("crashloop")

    response = client.post(
        f"/api/v1/alerts/{investigation_id}/feedback",
        json={"rating": "down", "notes": "blamed the probe"},
    )

    assert response.status_code == 200
    assert response.json()["rating"] == "down"


def test_a_bare_thumbs_down_is_accepted(client):
    """Requiring an explanation loses the signal from everyone without time to
    write one, and a rate from ratings people actually left beats a
    better-annotated one they did not."""
    investigation_id = _investigation()

    assert client.post(
        f"/api/v1/alerts/{investigation_id}/feedback", json={"rating": "down"}
    ).status_code == 200


def test_an_invalid_rating_is_a_400(client):
    investigation_id = _investigation()

    assert client.post(
        f"/api/v1/alerts/{investigation_id}/feedback", json={"rating": "sideways"}
    ).status_code == 400


def test_an_unknown_investigation_is_a_404(client):
    assert client.post(
        "/api/v1/alerts/nope/feedback", json={"rating": "up"}
    ).status_code == 404


def test_the_summary_endpoint_returns_playbooks(client):
    db.record_investigation_feedback(_investigation("crashloop"), "down", "wrong")

    body = client.get("/api/v1/alerts/feedback/summary").json()

    assert body["window_days"] == 30
    assert body["playbooks"][0]["playbook"] == "crashloop"


def test_the_summary_route_is_not_read_as_an_investigation_id(client):
    """`/feedback/summary` has to resolve before any dynamic segment, or
    `feedback` is taken for an id."""
    assert client.get("/api/v1/alerts/feedback/summary").status_code == 200
