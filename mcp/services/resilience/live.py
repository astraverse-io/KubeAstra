"""Live mode, pure half: what to fetch, and how to read what came back.

The caller (CLI, server) runs ``kubectl``; this module only names the resource
kinds to fetch and turns ``kubectl get nodes -o json`` into :class:`ClusterFacts`.
Keeping the subprocess out of the core is what keeps it testable and keyless.
"""
from __future__ import annotations

from typing import Any

from .types import ClusterFacts

# One `kubectl get <LIVE_KINDS> -A -o json` returns every object the rules read.
LIVE_KINDS = "deployments,statefulsets,daemonsets,jobs,cronjobs,horizontalpodautoscalers,poddisruptionbudgets,services"

ZONE_LABELS = ("topology.kubernetes.io/zone", "failure-domain.beta.kubernetes.io/zone")


def facts_from_nodes(nodes: Any) -> ClusterFacts:
    """``kubectl get nodes -o json`` → node count + distinct zones.

    Unschedulable (cordoned) nodes are left out: they can't take a rescheduled
    pod, so they don't count as somewhere to spread to. Zones are ``None`` when
    no node carries a zone label (bare-metal / kind), so RES005 doesn't claim a
    zone count it can't see.
    """
    items = nodes.get("items") if isinstance(nodes, dict) else None
    if not isinstance(items, list):
        return ClusterFacts()
    count = 0
    zones: set[str] = set()
    for n in items:
        if not isinstance(n, dict) or (n.get("spec") or {}).get("unschedulable"):
            continue
        count += 1
        labels = (n.get("metadata") or {}).get("labels") or {}
        for key in ZONE_LABELS:
            if labels.get(key):
                zones.add(str(labels[key]))
                break
    return ClusterFacts(nodes=count, zones=sorted(zones) or None)
