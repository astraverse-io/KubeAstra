"""Feedback audit persistence tests."""

from pathlib import Path
import sys
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import db  # noqa: E402
import auth  # noqa: E402
from routers import feedback  # noqa: E402
from routers.feedback import FeedbackRequest  # noqa: E402
from services.rag import promotion  # noqa: E402


def _init_temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "feedback-test.db"
    monkeypatch.setattr(db, "DB_PATH", str(db_path))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    db.init_db()
    return db_path


def _auth_request(user: dict):
    return SimpleNamespace(state=SimpleNamespace(user=auth.public_user(user)), cookies={})


def test_feedback_up_persists_audit_event(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)

    def fake_promote(capture_id, *, by_user=None):
        return {"ok": True, "runbook_id": capture_id, "title": "Kafka crashloop"}

    monkeypatch.setattr(promotion, "promote", fake_promote)

    response = feedback.submit_feedback(
        FeedbackRequest(
            capture_id="capture-1",
            rating="up",
            session_id="session-a",
            reason="good answer",
            prompt="Why is kafka crashlooping?",
            response="Kafka is failing because the JAR file is missing.",
            tool_used="investigate_pod",
        )
    )

    assert response.ok is True
    assert response.detail["feedback_event_id"]

    events = db.get_feedback_events(session_id="session-a")
    assert len(events) == 1
    assert events[0]["id"] == response.detail["feedback_event_id"]
    assert events[0]["capture_id"] == "capture-1"
    assert events[0]["rating"] == "up"
    assert events[0]["outcome"] == "accepted"
    assert events[0]["reason"] == "good answer"
    assert events[0]["prompt"] == "Why is kafka crashlooping?"
    assert events[0]["response"] == "Kafka is failing because the JAR file is missing."
    assert events[0]["tool_used"] == "investigate_pod"
    assert events[0]["action_result"]["runbook_id"] == "capture-1"


def test_feedback_down_persists_failed_audit_event(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)

    def fake_quarantine(capture_id, *, reason=None, by_user=None):
        return {"ok": False, "error": "capture_id not found"}

    monkeypatch.setattr(promotion, "quarantine", fake_quarantine)

    response = feedback.submit_feedback(
        FeedbackRequest(
            capture_id="missing-capture",
            rating="down",
            session_id="session-b",
            reason="wrong root cause",
        )
    )

    assert response.ok is False
    assert response.detail["feedback_event_id"]

    events = db.get_feedback_events(capture_id="missing-capture")
    assert len(events) == 1
    assert events[0]["session_id"] == "session-b"
    assert events[0]["rating"] == "down"
    assert events[0]["outcome"] == "failed"
    assert events[0]["error"] == "capture_id not found"


def test_feedback_invalid_rating_persists_rejected_audit_event(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)

    response = feedback.submit_feedback(
        FeedbackRequest(
            capture_id="capture-2",
            rating="maybe",
            session_id="session-c",
            reason="token: secret-token-value",
        )
    )

    assert response.ok is False
    assert response.detail["feedback_event_id"]

    events = feedback.list_feedback_events(session_id="session-c").events
    assert len(events) == 1
    assert events[0]["capture_id"] == "capture-2"
    assert events[0]["rating"] == "maybe"
    assert events[0]["outcome"] == "rejected"
    assert "secret-token-value" not in events[0]["reason"]


def test_feedback_without_capture_id_persists_audit_only_event(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)

    response = feedback.submit_feedback(
        FeedbackRequest(
            capture_id="message:session-a:assistant-1",
            rating="up",
            session_id="session-a",
            reason="useful answer",
            prompt="Any pods in CrashLoopBackOff?",
            response="Found 4 pods in CrashLoopBackOff.",
            tool_used="get_pods",
        )
    )

    assert response.ok is True
    assert response.detail["audit_only"] is True
    assert response.detail["feedback_event_id"]

    events = db.get_feedback_events(session_id="session-a")
    assert len(events) == 1
    assert events[0]["capture_id"] == "message:session-a:assistant-1"
    assert events[0]["rating"] == "up"
    assert events[0]["outcome"] == "accepted"
    assert events[0]["prompt"] == "Any pods in CrashLoopBackOff?"
    assert events[0]["response"] == "Found 4 pods in CrashLoopBackOff."
    assert events[0]["tool_used"] == "get_pods"
    assert events[0]["action_result"]["audit_only"] is True


def test_feedback_events_can_filter_across_all_sessions(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    db.save_feedback_event(
        capture_id="capture-up",
        rating="up",
        outcome="accepted",
        session_id="session-a",
        action_result={"ok": True},
    )
    db.save_feedback_event(
        capture_id="capture-down-failed",
        rating="down",
        outcome="failed",
        session_id="session-b",
        error="capture_id not found",
    )
    db.save_feedback_event(
        capture_id="capture-down-accepted",
        rating="down",
        outcome="accepted",
        session_id="session-c",
        action_result={"ok": True, "deleted": True},
    )

    down_events = feedback.list_feedback_events(rating="down").events
    failed_events = feedback.list_feedback_events(outcome="failed").events
    down_failed_events = feedback.list_feedback_events(rating="down", outcome="failed").events

    assert {event["capture_id"] for event in down_events} == {
        "capture-down-failed",
        "capture-down-accepted",
    }
    assert [event["capture_id"] for event in failed_events] == ["capture-down-failed"]
    assert [event["capture_id"] for event in down_failed_events] == ["capture-down-failed"]


def test_feedback_with_auth_requires_owned_session(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")

    owner = db.create_user(username="owner", password_hash=auth.hash_password("long-password"))
    intruder = db.create_user(username="intruder", password_hash=auth.hash_password("long-password"))
    session = db.create_session(user_id=owner["id"], title="Kafka crashloop")

    response = feedback.submit_feedback(
        FeedbackRequest(
            capture_id=f"message:{session['id']}:assistant-1",
            rating="down",
            session_id=session["id"],
            prompt="Why is kafka crashlooping?",
            response="The pod is failing because the JAR file is missing.",
            tool_used="investigate_pod",
        ),
        request=_auth_request(owner),
    )

    assert response.ok is True
    owner_events = feedback.list_feedback_events(
        request=_auth_request(owner),
        session_id=session["id"],
    ).events
    assert len(owner_events) == 1
    assert owner_events[0]["rating"] == "down"
    assert owner_events[0]["prompt"] == "Why is kafka crashlooping?"

    try:
        feedback.list_feedback_events(
            request=_auth_request(intruder),
            session_id=session["id"],
        )
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 404
    else:
        raise AssertionError("intruder should not be able to read another user's feedback")


def test_feedback_with_auth_rejects_missing_request(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")

    user = db.create_user(username="owner", password_hash=auth.hash_password("long-password"))
    session = db.create_session(user_id=user["id"], title="Kafka crashloop")

    try:
        feedback.submit_feedback(
            FeedbackRequest(
                capture_id=f"message:{session['id']}:assistant-1",
                rating="up",
                session_id=session["id"],
            )
        )
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 401
    else:
        raise AssertionError("auth-enabled feedback should require a request")
