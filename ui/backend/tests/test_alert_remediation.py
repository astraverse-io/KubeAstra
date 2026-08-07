"""What an alert may propose, and who has to agree.

This is the riskiest thing in the alert pipeline, so the tests are written
against the assumption that the flag will one day be on in production and
nobody will remember reading the code.

Everything here is about the gate, not the door: no test executes anything,
because nothing in this layer can. What is being pinned is that the layers only
ever subtract, that the default of every one of them is "nothing", and that
there is no path from a proposal to a changed cluster that does not pass
through a person.
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

import alert_remediation as rem  # noqa: E402
import db  # noqa: E402

BOTH = "rollout_restart,scale_deployment"


# ── the defaults permit nothing ───────────────────────────────────────────


def test_everything_off_permits_nothing():
    assert not rem.resolve_policy(enabled=False, global_actions=BOTH).permits_anything


def test_the_flag_alone_permits_nothing():
    """A single switch that enables writes against production is a switch
    somebody flips by accident. Turning it on has to be insufficient."""
    policy = rem.resolve_policy(enabled=True, global_actions="")

    assert not policy.permits_anything
    assert "allowed_actions is empty" in "; ".join(policy.reasons)


def test_the_reason_is_carried_so_a_refusal_is_actionable():
    """"Not permitted" alone sends an operator to read source code."""
    policy = rem.resolve_policy(enabled=False, global_actions=BOTH)

    assert policy.reasons


# ── layers may only subtract ──────────────────────────────────────────────


def test_a_playbook_cannot_grant_itself_something_global_policy_withholds():
    """Otherwise adding a playbook file would widen what the system can do to a
    cluster — an escalation by content, not configuration."""
    policy = rem.resolve_policy(
        enabled=True,
        global_actions="rollout_restart",
        playbook_actions="scale_deployment",
    )

    assert not policy.permits("scale_deployment")


def test_a_cluster_override_cannot_exceed_the_global_list():
    policy = rem.resolve_policy(
        enabled=True,
        global_actions="rollout_restart",
        cluster_actions=BOTH,
    )

    assert policy.allowed == {"rollout_restart"}


def test_permission_is_the_intersection_of_every_layer():
    policy = rem.resolve_policy(
        enabled=True,
        global_actions=BOTH,
        cluster_actions=BOTH,
        playbook_actions="rollout_restart",
    )

    assert policy.allowed == {"rollout_restart"}


def test_an_empty_cluster_override_excludes_that_cluster_entirely():
    """Distinct from having no override. Without the distinction there would be
    no way to exclude a single cluster from remediation."""
    assert not rem.resolve_policy(
        enabled=True, global_actions=BOTH, cluster_actions=""
    ).permits_anything


def test_no_cluster_override_inherits_the_global_list():
    policy = rem.resolve_policy(enabled=True, global_actions=BOTH, cluster_actions=None)

    assert policy.allowed == {"rollout_restart", "scale_deployment"}


# ── what may never be proposed ────────────────────────────────────────────


@pytest.mark.parametrize("action", ["delete_pod", "apply_patch", "exec_pod_command"])
def test_destructive_actions_are_never_automatically_proposable(action: str):
    """`delete_pod` discards the pod whose state is often the only evidence of
    what went wrong — and the alert is usually the reason to keep it.
    `apply_patch` takes an arbitrary body, so "which actions are allowed" stops
    being a finite question. Both stay available to a human in chat."""
    assert action not in rem.PROPOSABLE_ACTIONS

    policy = rem.resolve_policy(enabled=True, global_actions=f"{BOTH},{action}")
    assert action not in policy.allowed

    with pytest.raises(rem.RemediationNotPermitted):
        rem.check(action, policy)


def test_an_unknown_action_name_is_dropped_not_honoured():
    """A typo in configuration must not take ingestion down, and dropping can
    only ever permit less than intended."""
    policy = rem.resolve_policy(
        enabled=True, global_actions="rollout_restart,rollout_restrat"
    )

    assert policy.allowed == {"rollout_restart"}


def test_a_list_is_accepted_as_well_as_a_string():
    policy = rem.resolve_policy(
        enabled=True, global_actions=["rollout_restart", "scale_deployment"]
    )

    assert policy.allowed == {"rollout_restart", "scale_deployment"}


def test_check_passes_for_a_permitted_action():
    policy = rem.resolve_policy(enabled=True, global_actions=BOTH)

    rem.check("rollout_restart", policy)  # must not raise


# ── proposals ─────────────────────────────────────────────────────────────


@pytest.fixture
def clean_db():
    db.init_db()
    with db._conn() as con:
        con.execute("DELETE FROM remediation_proposals")
    yield db
    with db._conn() as con:
        con.execute("DELETE FROM remediation_proposals")


def _propose(ttl_seconds: int = 900, action: str = "rollout_restart") -> str:
    proposal_id = str(uuid.uuid4())
    db.create_remediation_proposal(
        proposal_id=proposal_id,
        investigation_id="inv-1",
        action=action,
        arguments={"namespace": "prod", "deployment_name": "api"},
        rationale="pods crashlooping since the 12:04 rollout",
        ttl_seconds=ttl_seconds,
    )
    return proposal_id


def test_a_proposal_starts_pending_and_changes_nothing(clean_db):
    proposal = db.get_remediation_proposal(_propose())

    assert proposal["status"] == "pending"
    assert proposal["decided_at"] is None


def test_the_rationale_is_kept(clean_db):
    """The person approving needs to know why, and "the model suggested it" is
    not why."""
    proposal = db.get_remediation_proposal(_propose())

    assert "12:04 rollout" in proposal["rationale"]


def test_approving_records_who_and_when(clean_db):
    proposal_id = _propose()

    decided = db.decide_remediation_proposal(proposal_id, True, "sre@example", "ok")

    assert decided["status"] == "approved"
    assert decided["decided_by"] == "sre@example"
    assert decided["decided_at"]


def test_rejecting_is_recorded_rather_than_deleted(clean_db):
    """"Who said no, and why" is exactly what gets asked after an outage that a
    rejected fix would have prevented."""
    proposal_id = _propose()

    decided = db.decide_remediation_proposal(
        proposal_id, False, "sre@example", "wrong deployment"
    )

    assert decided["status"] == "rejected"
    assert decided["decision_note"] == "wrong deployment"


def test_a_proposal_cannot_be_approved_twice(clean_db):
    proposal_id = _propose()
    db.decide_remediation_proposal(proposal_id, True, "a")

    assert db.decide_remediation_proposal(proposal_id, True, "b") is None


def test_a_rejected_proposal_cannot_later_be_approved(clean_db):
    proposal_id = _propose()
    db.decide_remediation_proposal(proposal_id, False, "a")

    assert db.decide_remediation_proposal(proposal_id, True, "b") is None


# ── approvals expire ──────────────────────────────────────────────────────


def test_a_pending_proposal_expires(clean_db):
    """Cluster state drifts. A proposal written about a cluster twenty minutes
    ago is about a cluster that no longer exists in that shape."""
    proposal_id = _propose(ttl_seconds=-1)

    assert db.get_remediation_proposal(proposal_id)["status"] == "expired"


def test_an_expired_proposal_cannot_be_approved(clean_db):
    proposal_id = _propose(ttl_seconds=-1)

    assert db.decide_remediation_proposal(proposal_id, True, "sre") is None


def test_expiry_needs_no_sweeper(clean_db):
    """Evaluated on read, so an expiry is in force the moment it passes even if
    nothing has run since."""
    proposal_id = _propose(ttl_seconds=-1)

    assert db.list_remediation_proposals(pending_only=True) == []
    assert db.get_remediation_proposal(proposal_id)["status"] == "expired"


def test_an_approval_expires_too(clean_db):
    """The dangerous case: approved, then left. Acting on it later would act on
    a judgement nobody would make now."""
    proposal_id = _propose(ttl_seconds=1)
    db.decide_remediation_proposal(proposal_id, True, "sre")
    with db._conn() as con:
        con.execute(
            "UPDATE remediation_proposals SET expires_at = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), proposal_id),
        )

    assert db.get_remediation_proposal(proposal_id)["status"] == "expired"
    assert db.mark_remediation_executed(proposal_id) is False


# ── execution is one-shot and needs an approval ───────────────────────────


def test_a_pending_proposal_cannot_be_executed(clean_db):
    """The property the whole feature rests on: no path from proposal to a
    changed cluster that does not pass through a person."""
    assert db.mark_remediation_executed(_propose()) is False


def test_a_rejected_proposal_cannot_be_executed(clean_db):
    proposal_id = _propose()
    db.decide_remediation_proposal(proposal_id, False, "sre")

    assert db.mark_remediation_executed(proposal_id) is False


def test_an_approved_proposal_can_be_executed_once(clean_db):
    proposal_id = _propose()
    db.decide_remediation_proposal(proposal_id, True, "sre")

    assert db.mark_remediation_executed(proposal_id) is True
    # A retried request must not restart a deployment twice.
    assert db.mark_remediation_executed(proposal_id) is False


def test_unparsable_arguments_are_not_silently_empty(clean_db):
    """An action with `{}` arguments could run against the wrong thing.
    Surfacing None makes it un-executable instead."""
    proposal_id = _propose()
    with db._conn() as con:
        con.execute(
            "UPDATE remediation_proposals SET arguments = 'not json' WHERE id = ?",
            (proposal_id,),
        )

    assert db.get_remediation_proposal(proposal_id)["arguments"] is None


def test_listing_by_investigation(clean_db):
    _propose()

    assert len(db.list_remediation_proposals(investigation_id="inv-1")) == 1
    assert db.list_remediation_proposals(investigation_id="other") == []


# ── the boundary this layer does not cross ────────────────────────────────


def test_this_module_cannot_execute_anything():
    """Deliberate boundary marker. This layer decides and records; execution
    lives in remediation_executor.py.

    Checked by parsing the module rather than grepping its text: a substring
    search matched the word "kubectl" in a comment explaining *why* a value is
    validated, and a guard that misfires on prose is a guard somebody weakens
    or deletes. What is actually forbidden is importing a way to act.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(rem))

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"subprocess", "os", "paramiko", "k8s", "services", "db"}
    assert not (imported & forbidden), (
        f"alert_remediation.py imports {sorted(imported & forbidden)} — this "
        f"layer must be able to decide, not to act"
    )


# ── the API ───────────────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch, clean_db):
    from fastapi.testclient import TestClient

    from main import app
    from routers import alerts as alerts_router

    monkeypatch.setenv("ALERT_AUTO_REMEDIATION_ENABLED", "true")
    monkeypatch.setenv("ALERT_AUTO_REMEDIATION_ALLOWED_ACTIONS", "rollout_restart")
    alerts_router.reset_webhook_settings()
    yield TestClient(app)
    alerts_router.reset_webhook_settings()


@pytest.fixture
def disabled_client(monkeypatch, clean_db):
    from fastapi.testclient import TestClient

    from main import app
    from routers import alerts as alerts_router

    monkeypatch.delenv("ALERT_AUTO_REMEDIATION_ENABLED", raising=False)
    monkeypatch.delenv("ALERT_AUTO_REMEDIATION_ALLOWED_ACTIONS", raising=False)
    alerts_router.reset_webhook_settings()
    yield TestClient(app)
    alerts_router.reset_webhook_settings()


BODY = {
    "investigation_id": "inv-1",
    "action": "rollout_restart",
    "arguments": {"namespace": "prod", "deployment_name": "api"},
    "rationale": "crashlooping since the 12:04 rollout",
}


def test_the_policy_is_readable_without_triggering_anything(client):
    """"Can it restart a deployment" should be answerable without firing an
    alert to find out."""
    body = client.get("/api/v1/remediation/policy").json()

    assert body["enabled"] is True
    assert body["allowed_actions"] == ["rollout_restart"]


def test_a_permitted_action_can_be_proposed(client):
    response = client.post("/api/v1/remediation/proposals", json=BODY)

    assert response.status_code == 201
    assert response.json()["status"] == "pending"


def test_an_action_outside_the_allowlist_is_refused(client):
    response = client.post(
        "/api/v1/remediation/proposals", json={**BODY, "action": "scale_deployment"}
    )

    assert response.status_code == 403
    assert "not permitted" in response.json()["detail"]


def test_nothing_can_be_proposed_while_the_feature_is_off(disabled_client):
    """The default state of every deployment."""
    response = disabled_client.post("/api/v1/remediation/proposals", json=BODY)

    assert response.status_code == 403
    assert disabled_client.get("/api/v1/remediation/policy").json()["allowed_actions"] == []


def test_a_rationale_is_required(client):
    response = client.post(
        "/api/v1/remediation/proposals", json={**BODY, "rationale": ""}
    )

    assert response.status_code == 422


def test_approving_through_the_api(client):
    proposal_id = client.post("/api/v1/remediation/proposals", json=BODY).json()["id"]

    decided = client.post(
        f"/api/v1/remediation/proposals/{proposal_id}/decision",
        json={"approve": True, "note": "confirmed the rollout"},
    )

    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"


def test_deciding_twice_is_a_conflict_not_a_silent_no_op(client):
    """An operator retrying a decision that can never take should be told
    why."""
    proposal_id = client.post("/api/v1/remediation/proposals", json=BODY).json()["id"]
    client.post(
        f"/api/v1/remediation/proposals/{proposal_id}/decision", json={"approve": True}
    )

    second = client.post(
        f"/api/v1/remediation/proposals/{proposal_id}/decision", json={"approve": False}
    )

    assert second.status_code == 409
    assert "approved" in second.json()["detail"]


def test_deciding_an_unknown_proposal_is_a_404(client):
    assert client.post(
        "/api/v1/remediation/proposals/nope/decision", json={"approve": True}
    ).status_code == 404


def test_pending_proposals_can_be_listed_for_an_investigation(client):
    client.post("/api/v1/remediation/proposals", json=BODY)

    body = client.get(
        "/api/v1/remediation/proposals",
        params={"investigation_id": "inv-1", "pending_only": True},
    ).json()

    assert body["count"] == 1


def test_this_router_never_executes_anything():
    """Deliberate marker, matching the one on the policy module.

    Approving records that an action is authorised. It does not run it —
    execution stays behind the confirmation-token machinery in
    services/plans.py. If this router ever grows the ability to act, the
    separation that makes the approval meaningful is gone.
    """
    import inspect

    from routers import remediation

    source = inspect.getsource(remediation)
    for forbidden in ("subprocess", "execute_step", "kubectl", "mark_remediation_executed"):
        assert forbidden not in source, (
            f"routers/remediation.py references {forbidden!r} — approving must "
            f"authorise, never act"
        )
