"""Live mode, pure half: what to fetch, and how to read what came back.

The caller (CLI, server) runs ``kubectl``; this module only names the resource
kinds to fetch and turns ``kubectl get nodes -o json`` into :class:`ClusterFacts`.
Keeping the subprocess out of the core is what keeps it testable and keyless.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from .types import ClusterFacts

# One `kubectl get <LIVE_KINDS> -A -o json` returns every object the rules read.
LIVE_KINDS = "deployments,statefulsets,daemonsets,jobs,cronjobs,horizontalpodautoscalers,poddisruptionbudgets,services"

ZONE_LABELS = ("topology.kubernetes.io/zone", "failure-domain.beta.kubernetes.io/zone")

# Namespaces that hold the cluster's own add-ons (CoreDNS, kube-proxy, CNI,
# provider agents). Their findings are real but not yours to fix, so live mode
# leaves them out of the score unless asked. Best-effort by name: `kube-*`
# covers upstream, the rest are common managed/local-dev defaults.
SYSTEM_NAMESPACE_PREFIXES = ("kube-", "gke-managed-", "openshift-")
SYSTEM_NAMESPACES = frozenset({"local-path-storage", "gmp-system", "gmp-public"})

RunJson = Callable[[list], Any]


def is_system_namespace(ns: str) -> bool:
    return ns in SYSTEM_NAMESPACES or ns.startswith(SYSTEM_NAMESPACE_PREFIXES)


def collect_live(
    run_json: RunJson,
    *,
    namespaces: Optional[Iterable[str]] = None,
    include_system: bool = False,
) -> tuple[list[dict], ClusterFacts, list[str]]:
    """Fetch the objects the rules read and the node facts, through the
    caller's kubectl runner. Returns ``(items, facts, notes)`` for
    :func:`~.engine.assess` (``items`` is a single ``kind: List``).

    ``namespaces=None`` is one ``--all-namespaces`` call; a list is one call
    per namespace (how a server with a namespace allowlist must read). System
    namespaces are dropped unless ``include_system`` — pass it when the user
    named the namespace themselves. ``run_json`` raises on failure; a failed
    read is an error, never an empty cluster.
    """
    if namespaces is None:
        lists = [run_json(["get", LIVE_KINDS, "--all-namespaces", "-o", "json"])]
    else:
        lists = [run_json(["get", LIVE_KINDS, "-n", ns, "-o", "json"]) for ns in namespaces]
    kept: list[dict] = []
    skipped: set[str] = set()
    for lst in lists:
        for item in (lst or {}).get("items") or []:
            if not isinstance(item, dict):
                continue
            ns = str((item.get("metadata") or {}).get("namespace") or "")
            if not include_system and ns and is_system_namespace(ns):
                skipped.add(ns)
                continue
            kept.append(item)
    facts = facts_from_nodes(run_json(["get", "nodes", "-o", "json"]))
    notes = []
    if skipped:
        notes.append(
            "Skipped system namespaces (" + ", ".join(sorted(skipped)) + ") — their add-ons aren't yours to fix. "
            "Include them with --include-system / include_system, or name the namespace."
        )
    return [{"apiVersion": "v1", "kind": "List", "items": kept}], facts, notes


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
