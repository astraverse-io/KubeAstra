"""Workload graph for the Mission Control topology map.

Nodes are workloads (Deployment, StatefulSet, DaemonSet) with a health colour.
Edges are optional service-to-service traffic, off unless Prometheus is
configured and `PROM_TRAFFIC_EDGES=true` — most clusters do not export the
Istio metrics it needs, and an empty graph is better than a slow one.

Flat module for the same reason as `cluster_summary` — see its docstring.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

WORKLOAD_KINDS = "deployments,statefulsets,daemonsets"

Health = Literal["green", "amber", "red", "idle"]


@dataclass
class ClusterTopology:
    nodes: list[dict]
    edges: list[dict]
    generated_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ClusterTopologyService:
    def __init__(self, cache_ttl_s: int = 30) -> None:
        self._cache: dict[tuple, tuple[float, ClusterTopology]] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._ttl = cache_ttl_s

    def invalidate(self, session_id: str) -> None:
        for key in [k for k in self._cache if k[0] == session_id]:
            self._cache.pop(key, None)

    async def get(
        self, session_id: str, conn: dict, *, scope: str, depth: int
    ) -> ClusterTopology:
        key = (session_id, scope, depth)
        hit = self._fresh(key)
        if hit is not None:
            return hit

        async with self._locks.setdefault(key, asyncio.Lock()):
            hit = self._fresh(key)
            if hit is not None:
                return hit
            fresh = await asyncio.to_thread(self._fetch, conn, scope)
            self._cache[key] = (time.time(), fresh)
            return fresh

    def _fresh(self, key: tuple) -> Optional[ClusterTopology]:
        cached = self._cache.get(key)
        if not cached or time.time() - cached[0] >= self._ttl:
            return None
        return cached[1]

    def _fetch(self, conn: dict, scope: str) -> ClusterTopology:
        from k8s.kubectl_runner import KubectlRunner

        namespace = (conn.get("namespace") or "default").strip() or "default"
        runner = KubectlRunner(
            kubeconfig_path=conn.get("kubeconfig_path"),
            context=conn.get("context_name"),
        )

        result = runner.run(
            ["get", WORKLOAD_KINDS, "-n", namespace, "-o", "json"]
        )
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            logger.warning("could not parse workload listing as JSON")
            payload = {}

        nodes = []
        for item in payload.get("items") or []:
            node = _to_node(item, namespace)
            if node is None:
                continue
            # "alerting" is the default scope: on a healthy cluster the map
            # should be empty rather than a wall of green boxes nobody reads.
            if scope == "alerting" and node["health"] in ("green", "idle"):
                continue
            nodes.append(node)

        return ClusterTopology(
            nodes=nodes,
            edges=_traffic_edges({n["id"] for n in nodes}),
            generated_at=datetime.now(timezone.utc).isoformat(),
        )


def _to_node(item: dict, fallback_namespace: str) -> Optional[dict]:
    meta = item.get("metadata") or {}
    name = meta.get("name")
    if not name:
        return None

    kind = item.get("kind") or "Workload"
    ready, desired = _replica_counts(item, kind)
    namespace = meta.get("namespace") or fallback_namespace

    return {
        "id": f"{namespace}/{kind}/{name}",
        "kind": kind,
        "namespace": namespace,
        "name": name,
        "health": _health(ready, desired),
        "replicas": {"ready": ready, "desired": desired},
    }


def _replica_counts(item: dict, kind: str) -> tuple[int, int]:
    """Ready and desired, per workload kind.

    DaemonSets have no `spec.replicas` — the count is decided by how many
    nodes match. Reading `replicas` for one yields 0, which would render every
    DaemonSet in the cluster as a red node forever.
    """
    status = item.get("status") or {}

    if kind == "DaemonSet":
        return (
            status.get("numberReady") or 0,
            status.get("desiredNumberScheduled") or 0,
        )

    spec_replicas = (item.get("spec") or {}).get("replicas")
    # `replicas` is optional and defaults to 1. Reading a missing value as 0
    # would make an unset Deployment look healthy whatever its pods are doing.
    desired = 1 if spec_replicas is None else spec_replicas
    return status.get("readyReplicas") or 0, desired


def _health(ready: int, desired: int) -> Health:
    """Colour for a workload, given what it has and what it wants.

    `desired == 0` is its own state, not a failure. A workload scaled to zero
    is doing exactly what it was told; colouring it red — as a naive
    `ready == 0` check does — puts a permanent alarm on the map for something
    nobody needs to fix.
    """
    if desired == 0:
        return "idle"
    if ready >= desired:
        return "green"
    if ready == 0:
        return "red"
    return "amber"


def _traffic_edges(node_ids: set[str]) -> list[dict]:
    """Service-to-service HTTP rates, when the cluster can supply them.

    Opt-in twice over — `PROM_TRAFFIC_EDGES` and a reachable Prometheus —
    because the query below needs Istio/Envoy request metrics that most
    clusters do not export. Returning no edges is a correct answer for them.
    """
    if os.environ.get("PROM_TRAFFIC_EDGES", "").strip().lower() not in ("1", "true", "yes"):
        return []

    try:
        from services.prometheus import query
    except Exception:
        logger.debug("prometheus service unavailable", exc_info=True)
        return []

    promql = (
        "sum by(source_workload_namespace,source_workload,"
        "destination_workload_namespace,destination_workload)"
        "(rate(istio_requests_total[5m]))"
    )
    try:
        series = (query(promql) or {}).get("data", {}).get("result", [])
    except Exception:
        # A missing metric is indistinguishable from a down Prometheus here,
        # and neither should cost the caller its topology.
        logger.warning("prometheus traffic query failed", exc_info=True)
        return []

    edges = []
    for entry in series:
        metric = entry.get("metric") or {}
        source = _workload_id(metric, "source")
        target = _workload_id(metric, "destination")
        # Only edges between workloads already on the map — an edge to a node
        # that was filtered out draws a line to nowhere.
        if source in node_ids and target in node_ids and source != target:
            edges.append(
                {
                    "source": source,
                    "target": target,
                    "kind": "http",
                    "rate_rps": _as_float(entry.get("value")),
                }
            )
    return edges


def _workload_id(metric: dict, side: str) -> str:
    """Istio labels → the node id shape used above.

    Istio reports workloads, which are Deployments in practice, so `Deployment`
    is hardcoded here. An edge whose id does not match a node is dropped by the
    caller, which is the correct outcome for anything else.
    """
    return (
        f"{metric.get(f'{side}_workload_namespace')}"
        f"/Deployment/{metric.get(f'{side}_workload')}"
    )


def _as_float(value: Any) -> float:
    # Prometheus returns [timestamp, "value"] — the number is a string.
    try:
        return float(value[1])
    except (TypeError, IndexError, ValueError):
        return 0.0
