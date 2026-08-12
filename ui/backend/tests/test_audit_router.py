"""The audit endpoints, and the call sites that feed them.

Two things are being checked here, and the second matters more:

  * the API returns what it claims and is read-only
  * the events that justify this feature are actually emitted — a trail that
    records tool calls but misses `mutation.executed` is worse than none,
    because it looks complete

The instrumentation tests call the real functions rather than asserting the
source contains `audit.emit`. A call site can be present and unreachable.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import audit  # noqa: E402
import db  # noqa: E402


@pytest.fixture
def client():
    from main import app

    db.init_db()
    with db._conn() as con:
        con.execute("DELETE FROM audit_events")
    yield TestClient(app)
    with db._conn() as con:
        con.execute("DELETE FROM audit_events")


# ── the endpoints ─────────────────────────────────────────────────────────


def test_every_endpoint_is_reachable(client):
    """A router that is written but never included is invisible until someone
    opens the UI. FastAPI defers route expansion here, so counting APIRoute
    objects does not prove registration — calling them does."""
    audit.emit(audit.EventType.MUTATION_EXECUTED, session_id="s1")

    for path in (
        "/api/v1/audit/events",
        "/api/v1/audit/replay/s1",
        "/api/v1/audit/verify",
        "/api/v1/audit/event-types",
        "/api/v1/audit/export",
    ):
        assert client.get(path).status_code == 200, path


def test_events_are_filtered_and_newest_first(client):
    audit.emit(audit.EventType.MUTATION_EXECUTED, cluster="prod", subject="first")
    audit.emit(audit.EventType.APPROVAL_GRANTED, cluster="dev", subject="second")
    audit.emit(audit.EventType.MUTATION_EXECUTED, cluster="prod", subject="third")

    body = client.get("/api/v1/audit/events?cluster=prod").json()

    assert [e["subject"] for e in body["events"]] == ["third", "first"]
    assert body["count"] == 2


def test_replay_is_oldest_first(client):
    for i in range(3):
        audit.emit(audit.EventType.TOOL_CALL_EXECUTED, session_id="s2", subject=f"t{i}")

    body = client.get("/api/v1/audit/replay/s2").json()

    assert [e["subject"] for e in body["events"]] == ["t0", "t1", "t2"]


def test_an_unknown_session_replays_empty_rather_than_404(client):
    """"This session did nothing auditable" and "this session does not exist"
    are different answers, and the audit trail is not the authority on which
    one applies."""
    response = client.get("/api/v1/audit/replay/never-existed")

    assert response.status_code == 200
    assert response.json()["events"] == []


def test_verify_explains_that_a_break_may_be_the_prune(client):
    """Pruning breaks the chain at the seam by design. A bare `ok: false`
    invites the reader to conclude tampering."""
    audit.emit(audit.EventType.ERROR)
    with db._conn() as con:
        con.execute("UPDATE audit_events SET payload = '{\"x\":1}'")

    body = client.get("/api/v1/audit/verify").json()

    assert body["ok"] is False
    assert "prune" in body["note"]


def test_export_is_jsonl_and_streams(client):
    for i in range(3):
        audit.emit(audit.EventType.ERROR, subject=f"e{i}")

    response = client.get("/api/v1/audit/export")

    assert response.headers["content-type"].startswith("application/x-ndjson")
    lines = [line for line in response.text.splitlines() if line.strip()]
    assert len(lines) == 3


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_the_api_cannot_write(client, method):
    """The value of the record is that the application cannot rewrite it."""
    response = getattr(client, method)("/api/v1/audit/events")

    assert response.status_code in (404, 405)


# ── instrumentation: the events that justify the feature ──────────────────


def test_executing_a_remediation_records_what_ran(client, monkeypatch):
    """The reason this feature exists. An approval says a human agreed; this
    says what actually happened to the cluster."""
    import remediation_executor

    monkeypatch.setattr(
        remediation_executor.db, "get_remediation_proposal",
        lambda pid: {"id": pid, "status": "approved", "action": "rollout_restart",
                     "arguments": {"namespace": "prod", "name": "api"},
                     "cluster_id": "", "approved_by": "pruthvi@example.com"},
    )
    monkeypatch.setattr(remediation_executor.alert_remediation, "check", lambda *a: None)
    monkeypatch.setattr(
        remediation_executor.alert_remediation, "validate_arguments",
        lambda action, args: {"namespace": "prod", "name": "api"},
    )
    monkeypatch.setattr(remediation_executor.alert_remediation, "check_namespace", lambda *a: None)
    monkeypatch.setattr(remediation_executor.db, "count_recent_remediations", lambda m: 0)
    monkeypatch.setattr(remediation_executor.db, "get_cluster", lambda cid: None)
    monkeypatch.setattr(remediation_executor.db, "mark_remediation_executed", lambda pid: True)
    monkeypatch.setattr(remediation_executor.db, "record_remediation_result", lambda *a: None)
    monkeypatch.setattr(remediation_executor, "_run", lambda action, args: {"success": True})
    monkeypatch.setattr(
        remediation_executor.cluster_execution, "routed_execution",
        lambda cluster: mock.MagicMock(__enter__=lambda s: None, __exit__=lambda s, *a: False),
    )

    remediation_executor.execute_proposal("p1")

    events = audit.query(event_type=audit.EventType.MUTATION_EXECUTED)
    assert len(events) == 1
    assert events[0]["payload"]["outcome"] == "executed"
    assert events[0]["payload"]["action"] == "rollout_restart"
    assert events[0]["actor_id"] == "pruthvi@example.com"


def test_a_failed_remediation_is_recorded_too(client, monkeypatch):
    """A trail that only holds successes answers "what worked". After an
    incident the question is "what was attempted"."""
    import remediation_executor

    monkeypatch.setattr(
        remediation_executor.db, "get_remediation_proposal",
        lambda pid: {"id": pid, "status": "approved", "action": "scale_deployment",
                     "arguments": {"namespace": "prod", "name": "api"},
                     "cluster_id": "", "approved_by": "pruthvi@example.com"},
    )
    monkeypatch.setattr(remediation_executor.alert_remediation, "check", lambda *a: None)
    monkeypatch.setattr(
        remediation_executor.alert_remediation, "validate_arguments",
        lambda action, args: {"namespace": "prod", "name": "api"},
    )
    monkeypatch.setattr(remediation_executor.alert_remediation, "check_namespace", lambda *a: None)
    monkeypatch.setattr(remediation_executor.db, "count_recent_remediations", lambda m: 0)
    monkeypatch.setattr(remediation_executor.db, "get_cluster", lambda cid: None)
    monkeypatch.setattr(remediation_executor.db, "mark_remediation_executed", lambda pid: True)
    monkeypatch.setattr(remediation_executor.db, "record_remediation_result", lambda *a: None)
    monkeypatch.setattr(
        remediation_executor, "_run",
        lambda action, args: (_ for _ in ()).throw(RuntimeError("kubectl exploded")),
    )
    monkeypatch.setattr(
        remediation_executor.cluster_execution, "routed_execution",
        lambda cluster: mock.MagicMock(__enter__=lambda s: None, __exit__=lambda s, *a: False),
    )

    with pytest.raises(remediation_executor.RemediationFailed):
        remediation_executor.execute_proposal("p2")

    events = audit.query(event_type=audit.EventType.MUTATION_EXECUTED)
    assert len(events) == 1
    assert events[0]["payload"]["outcome"] == "failed"
    assert events[0]["severity"] == "critical"


def test_a_broken_audit_does_not_break_the_mutation(client, monkeypatch):
    """The caller is mid-remediation. Recording is subordinate to acting.

    Written against a real failure — an unwritable database — rather than by
    monkeypatching emit() into raising. emit() catches everything internally
    precisely so it cannot propagate, so forcing it to raise would test a
    scenario that cannot occur and prove nothing about the guarantee.
    """
    import remediation_executor

    monkeypatch.setattr(
        remediation_executor.db, "get_remediation_proposal",
        lambda pid: {"id": pid, "status": "approved", "action": "rollout_restart",
                     "arguments": {"namespace": "prod", "name": "api"},
                     "cluster_id": "", "approved_by": "u"},
    )
    monkeypatch.setattr(remediation_executor.alert_remediation, "check", lambda *a: None)
    monkeypatch.setattr(
        remediation_executor.alert_remediation, "validate_arguments",
        lambda action, args: {"namespace": "prod", "name": "api"},
    )
    monkeypatch.setattr(remediation_executor.alert_remediation, "check_namespace", lambda *a: None)
    monkeypatch.setattr(remediation_executor.db, "count_recent_remediations", lambda m: 0)
    monkeypatch.setattr(remediation_executor.db, "get_cluster", lambda cid: None)
    monkeypatch.setattr(remediation_executor.db, "mark_remediation_executed", lambda pid: True)
    monkeypatch.setattr(remediation_executor.db, "record_remediation_result", lambda *a: None)
    monkeypatch.setattr(remediation_executor, "_run", lambda action, args: {"success": True})
    monkeypatch.setattr(
        remediation_executor.cluster_execution, "routed_execution",
        lambda cluster: mock.MagicMock(__enter__=lambda s: None, __exit__=lambda s, *a: False),
    )
    # The audit database is unreachable. The remediation must still run.
    monkeypatch.setattr(audit.db, "DB_PATH", "/nonexistent/dir/cannot/exist/audit.db")

    result = remediation_executor.execute_proposal("p3")

    assert result["proposal_id"] == "p3"
    assert result["action"] == "rollout_restart"
