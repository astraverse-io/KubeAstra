"""Carrying out an approved remediation.

Separate module on purpose. `alert_remediation` decides and `routers/remediation`
records; both have tests asserting they cannot act. This is the only place that
can, so there is exactly one file to read when asking "what can this system do
to a cluster".

Every check is re-run here, none are trusted from the proposal. An approval is
a statement about a moment: the policy may have been narrowed since, the
namespace allowlist may have changed, the cluster may have been disabled. The
approval says a human agreed — it does not say the action is still permitted.

Order matters. Cheap refusals come first so a proposal that was never going to
run does not consume the rate budget, and the approval is consumed *before*
kubectl is invoked, so a crash mid-execution cannot leave an approval that a
retry could spend a second time.
"""

from __future__ import annotations

import logging

import alert_remediation
import audit
import cluster_execution
import db

logger = logging.getLogger(__name__)


class RemediationFailed(RuntimeError):
    """The action was permitted but did not complete."""


def _settings():
    from config.settings import get_settings

    return get_settings()


def _current_policy() -> alert_remediation.Policy:
    settings = _settings()
    return alert_remediation.resolve_policy(
        enabled=settings.alert_auto_remediation_enabled,
        global_actions=settings.alert_auto_remediation_allowed_actions,
    )


def _run(action: str, args: dict) -> dict:
    """Invoke the write wrapper through the dry-run/confirm ritual it requires.

    The token comes from the wrapper's own dry run, so the same validation and
    the same audit trail apply as when a human does this from chat. Nothing
    here bypasses that path; it drives it.
    """
    from k8s import wrappers

    call = getattr(wrappers, action, None)
    if call is None:  # pragma: no cover — the allowlist prevents this
        raise RemediationFailed(f"no wrapper for {action!r}")

    preview = call(**args, dry_run=True)
    token = preview.get("confirmation_token")
    if not token:
        raise RemediationFailed(
            f"{action} dry run returned no confirmation token: "
            f"{preview.get('error') or preview}"
        )
    return call(**args, confirm=True, confirmation_token=token)


def execute_proposal(proposal_id: str) -> dict:
    """Run an approved proposal, re-checking everything first.

    Raises RemediationNotPermitted if policy now refuses it, and
    RemediationFailed if it was permitted but did not complete.
    """
    proposal = db.get_remediation_proposal(proposal_id)
    if proposal is None:
        raise alert_remediation.RemediationNotPermitted("no such proposal")

    if proposal["status"] != "approved":
        # Covers pending, rejected, already-executed and expired. Expiry is
        # computed on read, so a proposal approved and then left too long lands
        # here rather than running on a stale judgement.
        raise alert_remediation.RemediationNotPermitted(
            f"proposal is {proposal['status']}, not approved"
        )

    action = proposal["action"]
    # Re-resolved, not taken from the proposal: the allowlist may have been
    # narrowed since a human agreed to this.
    alert_remediation.check(action, _current_policy())

    args = alert_remediation.validate_arguments(action, proposal["arguments"])
    alert_remediation.check_namespace(
        args["namespace"], _settings().alert_remediation_allowed_namespaces
    )

    limit = _settings().alert_remediation_max_per_hour
    if db.count_recent_remediations(60) >= limit:
        # Before consuming the approval, so a rate-limited proposal stays
        # approved and can run once the window clears.
        raise alert_remediation.RemediationNotPermitted(
            f"remediation rate limit reached ({limit}/hour). This is a backstop "
            f"against a flapping alert acting repeatedly; approve again once it "
            f"is understood."
        )

    cluster = db.get_cluster(proposal["cluster_id"] or "")

    # Consume the approval before acting. A crash between here and the kubectl
    # call means the remediation did not happen and the approval is spent —
    # recoverable by approving again. The reverse ordering would let a retry
    # after a partial failure restart a deployment a second time.
    if not db.mark_remediation_executed(proposal_id):
        raise alert_remediation.RemediationNotPermitted(
            "approval could not be consumed — it was decided or expired "
            "between the check and the attempt"
        )

    logger.info(
        "executing remediation %s: %s in %s on cluster %s",
        proposal_id,
        action,
        args["namespace"],
        cluster.get("id") if cluster else "default",
    )

    cluster_id = (cluster or {}).get("id") or "default"
    # Recorded on every exit, including the ones that raise. A trail that only
    # holds successes answers "what worked", when the question after an
    # incident is "what was attempted".
    audit_common = {
        "actor_type": "agent",
        "actor_id": proposal.get("approved_by") or "agent",
        "cluster": cluster_id,
        "subject": f"{action} {args['namespace']}/{args.get('name', '')}".strip(),
    }

    try:
        with cluster_execution.routed_execution(cluster):
            result = _run(action, args)
    except cluster_execution.ClusterUnreachable as exc:
        db.record_remediation_result(proposal_id, "failed: cluster unreachable")
        audit.emit(
            audit.EventType.MUTATION_EXECUTED, **audit_common, severity="warn",
            payload={"proposal_id": proposal_id, "action": action,
                     "arguments": args, "outcome": "cluster unreachable",
                     "error": str(exc)},
        )
        raise
    except Exception as exc:
        db.record_remediation_result(proposal_id, f"failed: {exc}")
        audit.emit(
            audit.EventType.MUTATION_EXECUTED, **audit_common, severity="critical",
            payload={"proposal_id": proposal_id, "action": action,
                     "arguments": args, "outcome": "failed", "error": str(exc)},
        )
        raise RemediationFailed(str(exc)) from exc

    if not result.get("success", True):
        db.record_remediation_result(proposal_id, f"failed: {result.get('error')}")
        audit.emit(
            audit.EventType.MUTATION_EXECUTED, **audit_common, severity="critical",
            payload={"proposal_id": proposal_id, "action": action,
                     "arguments": args, "outcome": "failed",
                     "error": str(result.get("error"))},
        )
        raise RemediationFailed(str(result.get("error") or "action reported failure"))

    db.record_remediation_result(proposal_id, "executed")
    audit.emit(
        audit.EventType.MUTATION_EXECUTED, **audit_common, severity="warn",
        payload={"proposal_id": proposal_id, "action": action,
                 "arguments": args, "outcome": "executed", "result": result},
    )
    return {"proposal_id": proposal_id, "action": action, "result": result}
