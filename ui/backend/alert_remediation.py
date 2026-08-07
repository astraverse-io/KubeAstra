"""What an alert investigation is allowed to propose, and who has to say yes.

This is the gate, not the door. Nothing here executes anything — execution
stays in `services/plans.py`, behind the confirmation-token machinery it
already has. What this adds is the decision that has to be made *before* any of
that is reachable from an alert: given this playbook, on this cluster, is this
action permitted at all?

The whole design assumes the flag will one day be on in production and nobody
will remember reading this file. So:

**Everything defaults to nothing.** The feature flag, the deployment-wide
allowlist, the per-cluster list and the playbook's own declaration all default
to empty, and permission is their *intersection*. Turning on the flag alone
permits nothing. A single switch that enables writes against production is a
switch somebody flips by accident.

**No layer can widen another.** A playbook cannot grant itself an action the
deployment has not allowed, and a cluster override cannot exceed the global
list. Every layer can only subtract, so adding a playbook can never expand what
the system can do to a cluster.

**Nothing executes without a person.** Even a fully-permitted action produces a
proposal that a human has to approve, and the approval expires. There is no
configuration in which this proposes and executes in one step — that is a
property of the code, not of the defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The actions that could ever be permitted. Deliberately narrower than
# plans.ALLOWED_TOOLS, which also carries `delete_pod` and `apply_patch`:
#
#   * delete_pod is destructive in a way a restart is not — it discards a pod
#     whose state may be the only evidence of what went wrong, and the alert
#     that triggered it is often precisely the reason to keep it.
#   * apply_patch takes an arbitrary patch body, so "which actions are allowed"
#     stops being a finite question and becomes "whatever the model wrote".
#
# Both are still available to a human in chat, where a person composes the
# action and reads what it will do. Automatic proposal is a different bar.
PROPOSABLE_ACTIONS = frozenset({"rollout_restart", "scale_deployment"})


class RemediationNotPermitted(Exception):
    """Refused by policy. Carries the reason, because "not permitted" alone
    sends an operator to read source code."""


@dataclass(frozen=True)
class Policy:
    """The resolved answer for one (playbook, cluster) pair."""

    allowed: frozenset[str] = field(default_factory=frozenset)
    reasons: tuple[str, ...] = ()

    @property
    def permits_anything(self) -> bool:
        return bool(self.allowed)

    def permits(self, action: str) -> bool:
        return action in self.allowed


def _parse_actions(raw) -> frozenset[str]:
    """Accept a comma-separated string or a sequence; ignore unknown names.

    Unknown names are dropped rather than raising: a typo in a cluster override
    must not take alert ingestion down, and dropping is the safe direction —
    it can only ever permit less than intended, never more.
    """
    if not raw:
        return frozenset()
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
    else:
        parts = [str(p).strip() for p in raw]
    return frozenset(p for p in parts if p in PROPOSABLE_ACTIONS)


def resolve_policy(
    *,
    enabled: bool,
    global_actions,
    cluster_actions=None,
    playbook_actions=None,
) -> Policy:
    """Intersect every layer. Any one of them being empty permits nothing.

    `cluster_actions=None` means the cluster has no override and inherits the
    global list. An *empty* override is different and means "nothing here" —
    that distinction is the whole point of being able to write one, since
    otherwise there would be no way to exclude a single cluster.
    """
    reasons: list[str] = []

    if not enabled:
        return Policy(reasons=("alert_auto_remediation_enabled is off",))

    allowed = _parse_actions(global_actions)
    if not allowed:
        reasons.append(
            "alert_auto_remediation_allowed_actions is empty, so no action is "
            "permitted anywhere"
        )
        return Policy(reasons=tuple(reasons))

    if cluster_actions is not None:
        cluster_allowed = _parse_actions(cluster_actions)
        allowed &= cluster_allowed
        if not allowed:
            reasons.append("this cluster's override permits none of them")
            return Policy(reasons=tuple(reasons))

    if playbook_actions is not None:
        playbook_allowed = _parse_actions(playbook_actions)
        allowed &= playbook_allowed
        if not allowed:
            reasons.append("this playbook declares none of them")
            return Policy(reasons=tuple(reasons))

    return Policy(allowed=frozenset(allowed))


def check(action: str, policy: Policy) -> None:
    """Raise unless `action` is permitted. The reason travels with the refusal."""
    if action not in PROPOSABLE_ACTIONS:
        raise RemediationNotPermitted(
            f"{action!r} is never proposable automatically. Permitted: "
            f"{', '.join(sorted(PROPOSABLE_ACTIONS))}"
        )
    if not policy.permits(action):
        detail = "; ".join(policy.reasons) or (
            f"policy permits {sorted(policy.allowed)}"
        )
        raise RemediationNotPermitted(f"{action!r} not permitted: {detail}")


# ── Argument and namespace checks ─────────────────────────────────────────
#
# These cannot be shell-injected — the SSH runner quotes every argument before
# it builds a command string, which was verified rather than assumed. What they
# guard is the layer above that: a value that is perfectly safe as a shell
# token and still means something nobody intended once kubectl reads it.
#
# That matters much more when the node credential is broad. A wrong namespace
# is a wrong restart; with a cluster-admin credential it can be a restart of
# something holding the cluster up.

import re

# RFC 1123 label, which is what Kubernetes accepts for these names. Anything
# else — a wildcard, a flag, an empty string — is refused rather than passed
# through for kubectl to interpret.
_DNS_1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

MAX_NAME_LENGTH = 253
# A "scale to fix it" that scales to four thousand is not a fix. The cap is
# deliberately low: remediation restores service, it does not plan capacity.
MAX_REPLICAS = 50

_REQUIRED_ARGS = {
    "rollout_restart": ("namespace", "deployment_name"),
    "scale_deployment": ("namespace", "deployment_name", "replicas"),
}


def validate_arguments(action: str, arguments) -> dict:
    """Normalise and check a proposal's arguments, or raise."""
    if not isinstance(arguments, dict):
        # None is what the store returns for a row whose JSON did not parse.
        raise RemediationNotPermitted(
            "proposal arguments are missing or unreadable, so nothing can be "
            "executed from it"
        )

    required = _REQUIRED_ARGS.get(action)
    if required is None:
        raise RemediationNotPermitted(f"{action!r} has no argument contract")

    missing = [key for key in required if key not in arguments]
    if missing:
        raise RemediationNotPermitted(
            f"{action!r} is missing {', '.join(missing)}"
        )

    checked: dict = {}
    for key in ("namespace", "deployment_name"):
        if key not in required:
            continue
        value = str(arguments[key]).strip()
        if not value or len(value) > MAX_NAME_LENGTH or not _DNS_1123.match(value):
            raise RemediationNotPermitted(
                f"{key} {arguments[key]!r} is not a valid Kubernetes name"
            )
        checked[key] = value

    if "replicas" in required:
        raw = arguments["replicas"]
        # bool is an int in Python, and `replicas: true` scaling to 1 is not a
        # decision anybody made.
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise RemediationNotPermitted(f"replicas {raw!r} is not an integer")
        if not 0 <= raw <= MAX_REPLICAS:
            raise RemediationNotPermitted(
                f"replicas {raw} is outside 0..{MAX_REPLICAS}"
            )
        checked["replicas"] = raw

    return checked


def parse_namespaces(raw) -> frozenset[str]:
    if not raw:
        return frozenset()
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
    else:
        parts = [str(p).strip() for p in raw]
    return frozenset(p for p in parts if p)


def check_namespace(namespace: str, allowed) -> None:
    """Raise unless remediation is permitted in this namespace.

    Empty means none, matching every other layer. There is deliberately no
    wildcard: with a broad node credential, "all namespaces" is a setting
    somebody would enable to get unblocked and never revisit.
    """
    permitted = parse_namespaces(allowed)
    if not permitted:
        raise RemediationNotPermitted(
            "alert_remediation_allowed_namespaces is empty, so remediation is "
            "not permitted in any namespace"
        )
    if namespace not in permitted:
        raise RemediationNotPermitted(
            f"namespace {namespace!r} is not in the remediation allowlist"
        )
