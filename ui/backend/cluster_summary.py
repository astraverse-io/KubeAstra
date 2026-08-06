"""Live cluster counters for the Mission Control header.

Two numbers the header shows continuously — pods ready, alerts firing —
which means this runs every 30 seconds per open tab. So it caches, and it
single-flights: N tabs on one session produce one `kubectl` call, not N.

Flat module rather than `services/cluster_summary.py`, which the design doc
asked for. `main.py` puts `mcp/` first on `sys.path` and `mcp/services/` is a
real package with ~70 import sites, so `import services.cluster_summary`
resolves into *that* package and raises ModuleNotFoundError. The rest of this
directory (`auth`, `db`, `memory`, `cluster_session`) is flat for the same
reason.

Alert counts come from the local `investigations` table, not a live
Alertmanager query. There is no server-mode Alertmanager setting to read —
only `desktop_config`'s per-user one — so the documented approach would have
pinned these to zero forever. Counting what KubeAstra has actually
investigated needs no new configuration, works in both modes, and is a
SQLite read rather than a network round trip inside a 30-second poll.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import db

logger = logging.getLogger(__name__)

# How far back an alert still counts as "active" on the header. Alerts have no
# resolved-at column, so recency is the only signal available; a day is long
# enough to survive an overnight incident and short enough that last week's
# noise does not sit on the header forever.
ACTIVE_WINDOW = timedelta(hours=24)

# Severity spellings seen in the wild. Alertmanager sends `critical`; Grafana
# and some in-house routers send `sev1` or `sev-1`.
SEV1_VALUES = frozenset({"critical", "sev1", "sev-1", "p1"})


@dataclass
class ClusterSummary:
    cluster: Optional[str]
    context: Optional[str]
    namespace: str
    counters: dict[str, int]
    generated_at: str
    cache_age_seconds: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class RBACError(RuntimeError):
    """The credential cannot list pods in the namespace it was pointed at.

    Distinct from a failure: the cluster answered, and the answer was no. The
    header shows "insufficient access" rather than an error, because nothing
    is broken.
    """


class ClusterSummaryService:
    def __init__(self, cache_ttl_s: int = 30) -> None:
        self._cache: dict[str, tuple[float, ClusterSummary]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._ttl = cache_ttl_s

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    def invalidate(self, session_id: str) -> None:
        """Drop a session's cached summary — used when the cluster changes."""
        self._cache.pop(session_id, None)

    async def get(self, session_id: str, conn: dict) -> ClusterSummary:
        hit = self._fresh(session_id)
        if hit is not None:
            return hit

        async with self._lock_for(session_id):
            # Re-check inside the lock: whoever held it was most likely
            # fetching the very thing this caller is waiting for.
            hit = self._fresh(session_id)
            if hit is not None:
                return hit

            # kubectl is a blocking subprocess. Without the thread it stalls
            # the event loop for every other request for the duration.
            fresh = await asyncio.to_thread(self._fetch, conn)
            self._cache[session_id] = (time.time(), fresh)
            return fresh

    def _fresh(self, session_id: str) -> Optional[ClusterSummary]:
        cached = self._cache.get(session_id)
        if not cached:
            return None
        age = time.time() - cached[0]
        if age >= self._ttl:
            return None
        return _with_age(cached[1], int(age))

    # ── the blocking half ─────────────────────────────────────────────────

    def _fetch(self, conn: dict) -> ClusterSummary:
        from k8s.kubectl_runner import KubectlRunner

        namespace = (conn.get("namespace") or "default").strip() or "default"
        runner = KubectlRunner(
            kubeconfig_path=conn.get("kubeconfig_path"),
            context=conn.get("context_name"),
        )

        self._preflight(runner, namespace)

        pods_ready, pods_total = _count_pods(runner, namespace)
        degraded = _count_degraded_workloads(runner, namespace)
        alerts_active, alerts_sev1 = _count_alerts(namespace)

        return ClusterSummary(
            cluster=conn.get("cluster_name"),
            context=conn.get("context_name"),
            namespace=namespace,
            counters={
                "pods_ready": pods_ready,
                "pods_total": pods_total,
                "workloads_degraded": degraded,
                "alerts_active": alerts_active,
                "alerts_sev1": alerts_sev1,
            },
            generated_at=datetime.now(timezone.utc).isoformat(),
            cache_age_seconds=0,
        )

    def _preflight(self, runner, namespace: str) -> None:
        """Ask before looking, so a denial reads as a denial.

        Without this, a read-only credential produces a `kubectl` failure that
        surfaces as a 502 — indistinguishable from the cluster being down.
        """
        try:
            result = runner.run(["auth", "can-i", "list", "pods", "-n", namespace])
        except Exception as exc:
            raise RBACError(f"could not check access to {namespace}") from exc

        if (result.stdout or "").strip().lower() != "yes":
            raise RBACError(f"cannot list pods in {namespace}")


def _count_pods(runner, namespace: str) -> tuple[int, int]:
    items = _get_items(runner, "pods", namespace)
    ready = sum(1 for item in items if _pod_is_ready(item))
    return ready, len(items)


def _count_degraded_workloads(runner, namespace: str) -> int:
    degraded = 0
    for item in _get_items(runner, "deployments", namespace):
        spec_replicas = item.get("spec", {}).get("replicas")
        # `replicas` is optional and defaults to 1. Treating a missing value as
        # 0 would make every unset Deployment look healthy no matter what.
        wanted = 1 if spec_replicas is None else spec_replicas
        ready = item.get("status", {}).get("readyReplicas") or 0
        if ready < wanted:
            degraded += 1
    return degraded


def _get_items(runner, kind: str, namespace: str) -> list[dict]:
    result = runner.run(["get", kind, "-n", namespace, "-o", "json"])
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        # Truncated output is the expected cause — the runner caps response
        # size. A partial count is worse than none, because it looks correct.
        logger.warning("could not parse `kubectl get %s` as JSON", kind)
        return []
    items = payload.get("items")
    return items if isinstance(items, list) else []


def _pod_is_ready(item: dict) -> bool:
    conditions = item.get("status", {}).get("conditions") or []
    return any(
        c.get("type") == "Ready" and c.get("status") == "True" for c in conditions
    )


def _count_alerts(namespace: str) -> tuple[int, int]:
    """Alerts KubeAstra has investigated recently, and how many were sev-1.

    Namespace-scoped to match the pod counters beside it: a header reading
    "12 pods / 40 alerts" where the alerts are cluster-wide invites the
    conclusion that those 40 alerts are about those 12 pods.
    """
    since = (datetime.now(timezone.utc) - ACTIVE_WINDOW).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with db._conn() as con:
            rows = con.execute(
                "SELECT severity FROM investigations "
                "WHERE namespace = ? AND created_at >= ?",
                (namespace, since),
            ).fetchall()
    except Exception:
        # The header is decoration; a counter that cannot be read must not
        # take down the panel that shows the pod counts next to it.
        logger.warning("could not read investigation counts", exc_info=True)
        return 0, 0

    severities = [(row[0] or "").strip().lower() for row in rows]
    return len(severities), sum(1 for s in severities if s in SEV1_VALUES)


def _with_age(summary: ClusterSummary, age: int) -> ClusterSummary:
    return ClusterSummary(**{**summary.as_dict(), "cache_age_seconds": age})
