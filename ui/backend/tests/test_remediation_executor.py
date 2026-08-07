"""The only code in the alert pipeline that can change a cluster.

The node credential is cluster-admin, so the allowlist here is not defence in
depth — it is the defence. Every test below is written on that basis: the
question is never "is this convenient", it is "if this check were missing, what
could a crafted alert or a bug reach".

Nothing is trusted from the approval. An approval is a statement about a
moment; by the time it is spent, the policy may have been narrowed, the
namespace allowlist changed, or the cluster disabled.
"""

from __future__ import annotations

import sys
import uuid
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
import remediation_executor as executor  # noqa: E402


@pytest.fixture
def clean_db():
    db.init_db()
    with db._conn() as con:
        con.execute("DELETE FROM remediation_proposals")
        con.execute("DELETE FROM cluster_registry")
    yield db
    with db._conn() as con:
        con.execute("DELETE FROM remediation_proposals")
        con.execute("DELETE FROM cluster_registry")


@pytest.fixture
def permissive(monkeypatch):
    monkeypatch.setenv("ALERT_AUTO_REMEDIATION_ENABLED", "true")
    monkeypatch.setenv("ALERT_AUTO_REMEDIATION_ALLOWED_ACTIONS", "rollout_restart,scale_deployment")
    monkeypatch.setenv("ALERT_REMEDIATION_ALLOWED_NAMESPACES", "prod,staging")
    monkeypatch.setenv("ALERT_REMEDIATION_MAX_PER_HOUR", "5")
    from config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def wrapper_calls(monkeypatch):
    """Stand in for the kubectl write wrappers, recording what was asked."""
    calls = []

    def make(name):
        def call(**kwargs):
            calls.append({"action": name, **kwargs})
            if kwargs.get("dry_run"):
                return {"success": True, "confirmation_token": "tok-123"}
            return {"success": True, "output": f"{name} done"}

        return call

    from k8s import wrappers

    monkeypatch.setattr(wrappers, "rollout_restart", make("rollout_restart"))
    monkeypatch.setattr(wrappers, "scale_deployment", make("scale_deployment"))
    return calls


def _approved(
    action: str = "rollout_restart",
    arguments: dict | None = None,
    approve: bool = True,
) -> str:
    proposal_id = str(uuid.uuid4())
    db.create_remediation_proposal(
        proposal_id=proposal_id,
        investigation_id="inv-1",
        action=action,
        arguments=arguments
        if arguments is not None
        else {"namespace": "prod", "deployment_name": "api"},
        rationale="crashlooping",
        ttl_seconds=900,
    )
    if approve:
        db.decide_remediation_proposal(proposal_id, True, "sre@example")
    return proposal_id


# ── the happy path, so the refusals below mean something ──────────────────


def test_an_approved_permitted_proposal_runs(clean_db, permissive, wrapper_calls):
    result = executor.execute_proposal(_approved())

    assert result["action"] == "rollout_restart"
    assert [c["action"] for c in wrapper_calls] == ["rollout_restart", "rollout_restart"]


def test_it_drives_the_dry_run_then_confirm_ritual(clean_db, permissive, wrapper_calls):
    """Not a bypass of the write path — it drives it, so the same validation
    and the same audit trail apply as when a human does this from chat."""
    executor.execute_proposal(_approved())

    assert wrapper_calls[0]["dry_run"] is True
    assert wrapper_calls[1]["confirm"] is True
    assert wrapper_calls[1]["confirmation_token"] == "tok-123"


# ── nothing is trusted from the approval ──────────────────────────────────


def test_a_narrowed_policy_stops_an_already_approved_proposal(
    clean_db, permissive, wrapper_calls, monkeypatch
):
    """The approval said a human agreed then. It does not say the deployment
    still permits it now."""
    proposal_id = _approved()
    monkeypatch.setenv("ALERT_AUTO_REMEDIATION_ALLOWED_ACTIONS", "scale_deployment")
    from config.settings import get_settings

    get_settings.cache_clear()

    with pytest.raises(rem.RemediationNotPermitted):
        executor.execute_proposal(proposal_id)

    assert wrapper_calls == []


def test_turning_the_feature_off_stops_approved_proposals(
    clean_db, permissive, wrapper_calls, monkeypatch
):
    """The flag has to be a kill switch, not just a gate at proposal time —
    that is what someone reaches for when something is going wrong."""
    proposal_id = _approved()
    monkeypatch.setenv("ALERT_AUTO_REMEDIATION_ENABLED", "false")
    from config.settings import get_settings

    get_settings.cache_clear()

    with pytest.raises(rem.RemediationNotPermitted):
        executor.execute_proposal(proposal_id)

    assert wrapper_calls == []


def test_a_namespace_outside_the_allowlist_is_refused(clean_db, permissive, wrapper_calls):
    """With a cluster-admin credential this is the difference between
    restarting the wrong application and restarting kube-system."""
    proposal_id = _approved(
        arguments={"namespace": "kube-system", "deployment_name": "coredns"}
    )

    with pytest.raises(rem.RemediationNotPermitted) as exc:
        executor.execute_proposal(proposal_id)

    assert "kube-system" in str(exc.value)
    assert wrapper_calls == []


def test_an_empty_namespace_allowlist_permits_nothing(
    clean_db, permissive, wrapper_calls, monkeypatch
):
    monkeypatch.setenv("ALERT_REMEDIATION_ALLOWED_NAMESPACES", "")
    from config.settings import get_settings

    get_settings.cache_clear()

    with pytest.raises(rem.RemediationNotPermitted):
        executor.execute_proposal(_approved())

    assert wrapper_calls == []


# ── approval state ────────────────────────────────────────────────────────


@pytest.mark.parametrize("approve", [False])
def test_a_rejected_proposal_cannot_run(clean_db, permissive, wrapper_calls, approve):
    proposal_id = _approved(approve=approve)
    db.decide_remediation_proposal(proposal_id, False, "sre")

    with pytest.raises(rem.RemediationNotPermitted):
        executor.execute_proposal(proposal_id)


def test_a_pending_proposal_cannot_run(clean_db, permissive, wrapper_calls):
    """The property the whole feature rests on."""
    with pytest.raises(rem.RemediationNotPermitted) as exc:
        executor.execute_proposal(_approved(approve=False))

    assert "not approved" in str(exc.value)
    assert wrapper_calls == []


def test_running_twice_does_not_act_twice(clean_db, permissive, wrapper_calls):
    """A retried request must not restart a deployment a second time."""
    proposal_id = _approved()
    executor.execute_proposal(proposal_id)

    with pytest.raises(rem.RemediationNotPermitted):
        executor.execute_proposal(proposal_id)

    assert [c for c in wrapper_calls if c.get("confirm")] == wrapper_calls[1:2]


def test_an_unknown_proposal_is_refused(clean_db, permissive):
    with pytest.raises(rem.RemediationNotPermitted):
        executor.execute_proposal("no-such-proposal")


# ── argument validation ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "arguments",
    [
        {"namespace": "prod", "deployment_name": "--all"},
        {"namespace": "prod", "deployment_name": "*"},
        {"namespace": "prod", "deployment_name": ""},
        {"namespace": "prod", "deployment_name": "API"},
        {"namespace": "prod/other", "deployment_name": "api"},
    ],
)
def test_a_name_kubectl_would_read_as_something_else_is_refused(
    clean_db, permissive, wrapper_calls, arguments
):
    """These cannot break out of the shell — the SSH runner quotes every
    argument. What they can do is mean something to kubectl that nobody
    intended, which with a broad credential is the whole risk."""
    with pytest.raises(rem.RemediationNotPermitted):
        executor.execute_proposal(_approved(arguments=arguments))

    assert wrapper_calls == []


@pytest.mark.parametrize("replicas", [-1, 10_000, "3", True, None])
def test_an_unreasonable_replica_count_is_refused(
    clean_db, permissive, wrapper_calls, replicas
):
    """A "scale to fix it" that scales to ten thousand is not a fix. `True` is
    included because bool is an int in Python and `replicas: true` scaling to 1
    is not a decision anybody made."""
    proposal_id = _approved(
        action="scale_deployment",
        arguments={"namespace": "prod", "deployment_name": "api", "replicas": replicas},
    )

    with pytest.raises(rem.RemediationNotPermitted):
        executor.execute_proposal(proposal_id)

    assert wrapper_calls == []


def test_a_valid_scale_is_allowed_through(clean_db, permissive, wrapper_calls):
    proposal_id = _approved(
        action="scale_deployment",
        arguments={"namespace": "prod", "deployment_name": "api", "replicas": 3},
    )

    executor.execute_proposal(proposal_id)

    assert wrapper_calls[1]["replicas"] == 3


def test_unreadable_arguments_are_refused(clean_db, permissive, wrapper_calls):
    proposal_id = _approved()
    with db._conn() as con:
        con.execute(
            "UPDATE remediation_proposals SET arguments = 'not json' WHERE id = ?",
            (proposal_id,),
        )

    with pytest.raises(rem.RemediationNotPermitted):
        executor.execute_proposal(proposal_id)

    assert wrapper_calls == []


# ── the rate cap ──────────────────────────────────────────────────────────


def test_the_hourly_cap_stops_a_flapping_alert_acting_repeatedly(
    clean_db, permissive, wrapper_calls, monkeypatch
):
    monkeypatch.setenv("ALERT_REMEDIATION_MAX_PER_HOUR", "2")
    from config.settings import get_settings

    get_settings.cache_clear()

    executor.execute_proposal(_approved())
    executor.execute_proposal(_approved())

    with pytest.raises(rem.RemediationNotPermitted) as exc:
        executor.execute_proposal(_approved())

    assert "rate limit" in str(exc.value)


def test_a_rate_limited_proposal_stays_approved(
    clean_db, permissive, wrapper_calls, monkeypatch
):
    """The cap is checked before the approval is consumed, so a proposal
    refused for rate can run once the window clears instead of needing a fresh
    human decision."""
    monkeypatch.setenv("ALERT_REMEDIATION_MAX_PER_HOUR", "0")
    from config.settings import get_settings

    get_settings.cache_clear()

    proposal_id = _approved()
    with pytest.raises(rem.RemediationNotPermitted):
        executor.execute_proposal(proposal_id)

    assert db.get_remediation_proposal(proposal_id)["status"] == "approved"


# ── failure handling ──────────────────────────────────────────────────────


def test_the_approval_is_spent_before_kubectl_runs(clean_db, permissive, monkeypatch):
    """A crash between consuming the approval and the action means the
    remediation did not happen and the approval is gone — recoverable by
    approving again. The reverse ordering would let a retry after a partial
    failure act twice, which is not recoverable.
    """
    from k8s import wrappers

    def explode(**kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(wrappers, "rollout_restart", explode)
    proposal_id = _approved()

    with pytest.raises(executor.RemediationFailed):
        executor.execute_proposal(proposal_id)

    assert db.get_remediation_proposal(proposal_id)["status"] == "executed"


def test_a_failure_is_recorded_on_the_proposal(clean_db, permissive, monkeypatch):
    """The row should be the whole story: proposed, approved by whom, and what
    happened."""
    from k8s import wrappers

    monkeypatch.setattr(
        wrappers, "rollout_restart", lambda **k: {"success": False, "error": "forbidden"}
    )
    proposal_id = _approved()

    with pytest.raises(executor.RemediationFailed):
        executor.execute_proposal(proposal_id)

    assert "forbidden" in db.get_remediation_proposal(proposal_id)["decision_note"]


def test_a_missing_confirmation_token_stops_the_action(clean_db, permissive, monkeypatch):
    """If the dry run will not issue a token, the write path is refusing this —
    running anyway would be forcing past the machinery that said no."""
    from k8s import wrappers

    monkeypatch.setattr(
        wrappers, "rollout_restart", lambda **k: {"success": True, "error": "not allowed"}
    )

    with pytest.raises(executor.RemediationFailed):
        executor.execute_proposal(_approved())


def test_an_unreachable_cluster_does_not_run_against_the_default(
    clean_db, permissive, wrapper_calls
):
    """Same rule as investigation: never fall back. Remediating the wrong
    cluster is worse than failing to remediate."""
    db.register_cluster("prod-eu", "eu.example", "kubeastra", "absent-secret")
    proposal_id = str(uuid.uuid4())
    db.create_remediation_proposal(
        proposal_id=proposal_id,
        investigation_id="inv-1",
        action="rollout_restart",
        arguments={"namespace": "prod", "deployment_name": "api"},
        rationale="x",
        ttl_seconds=900,
        cluster_id="prod-eu",
    )
    db.decide_remediation_proposal(proposal_id, True, "sre")

    import cluster_execution

    with pytest.raises(cluster_execution.ClusterUnreachable):
        executor.execute_proposal(proposal_id)

    assert [c for c in wrapper_calls if c.get("confirm")] == []
