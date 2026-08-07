"""Deciding which cluster an alert is about.

One assistant serves several clusters, each with its own Prometheus pointing at
it. Without routing, every alert was investigated against whatever target the
backend happened to be aimed at — so an alert from staging produced a
confident, fully-evidenced root-cause answer about production.

That is the failure this exists to stop, and it is worse than no answer,
because nothing about a wrong-cluster answer looks wrong. The evidence is real,
the reasoning is sound, and it is about a different machine.

So the rule is: investigate the right cluster, or investigate nothing and say
why. Never guess.
"""

from __future__ import annotations

from dataclasses import dataclass

# The label Prometheus is expected to attach via `external_labels`. One line of
# config per cluster, and it is the only thing that makes routing possible —
# nothing else in an alert payload reliably identifies where it came from.
CLUSTER_LABEL = "cluster"


@dataclass(frozen=True)
class Route:
    """Where an alert should be investigated.

    `cluster` is None for the default target — either single-cluster mode, or a
    manual run that is aimed at the backend's own context.
    """

    cluster: dict | None
    investigate: bool
    reason: str = ""

    @property
    def cluster_id(self) -> str:
        return str(self.cluster["id"]) if self.cluster else ""


DEFAULT_TARGET = Route(cluster=None, investigate=True, reason="default target")


def resolve(
    labels: dict[str, str],
    *,
    registry_is_empty: bool,
    lookup,
    is_manual: bool = False,
) -> Route:
    """Route an alert, or refuse to.

    `lookup` takes a cluster id and returns its registry row or None. It is
    passed in rather than imported so this stays a pure decision that can be
    reasoned about without a database.
    """
    # Single-cluster mode. Every existing deployment is here, and nothing about
    # its behaviour may change: cluster labels are ignored entirely rather than
    # half-honoured, which would make an unregistered label start failing
    # alerts that work today.
    if registry_is_empty:
        return DEFAULT_TARGET

    # A manual investigation is started by a person in the UI, against the
    # cluster the backend is already pointed at. It carries no `cluster` label
    # because there is nothing to carry one — so applying the strict rule below
    # would mean that registering your first cluster silently broke every
    # manual run. A manual request that *does* name a cluster still routes.
    if is_manual and not labels.get(CLUSTER_LABEL):
        return DEFAULT_TARGET

    cluster_id = str(labels.get(CLUSTER_LABEL, "")).strip()
    if not cluster_id:
        return Route(
            cluster=None,
            investigate=False,
            reason=(
                "alert carries no `cluster` label and this deployment serves "
                "several clusters. Set `external_labels: {cluster: <id>}` in "
                "the sending Prometheus."
            ),
        )

    cluster = lookup(cluster_id)
    if not cluster:
        return Route(
            cluster=None,
            investigate=False,
            reason=(
                f"no cluster registered as {cluster_id!r}. Register it at "
                f"POST /api/v1/clusters, or correct the sender's `cluster` label."
            ),
        )

    if cluster.get("status") != "active":
        # Disabled is a deliberate act, so this is not an error — but it must
        # not silently fall back to the default target either, which would send
        # the investigation to the wrong machine.
        return Route(
            cluster=None,
            investigate=False,
            reason=f"cluster {cluster_id!r} is disabled",
        )

    return Route(cluster=cluster, investigate=True)
