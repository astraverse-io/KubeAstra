"""Resolve which cluster a session targets — and refuse to guess.

`k8s.kubectl_runner.get_runner()` returns `runner_ctx.get() or kubectl`. That
fallback is correct for a session that never connected to anything: use the
machine's own kubeconfig. It is dangerous for a session that *did* connect and
whose runner then failed to install, because kubectl still succeeds — against
whatever cluster the local context happens to point at. On a laptop that is
the operator's day-job cluster.

So this module draws the distinction the boolean never could:

  * no connection row            -> None. Local kubeconfig, as intended.
  * row present, kubeconfig gone -> raise. Never silently retarget.

The stale row is deliberately **not** deleted. Deleting it would make the next
message look like "never connected" and quietly resume using local
credentials — reopening the exact hole this closes. It keeps failing until the
operator reconnects or disconnects.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import db

logger = logging.getLogger(__name__)


class ClusterConnectionUnavailable(RuntimeError):
    """A session's cluster is configured but no longer reachable.

    Raised instead of falling back, so a command never runs somewhere the
    operator did not choose.
    """

    def __init__(self, cluster: str, reason: str) -> None:
        self.cluster = cluster
        self.reason = reason
        super().__init__(
            f"The kubeconfig for cluster '{cluster}' is no longer available, so "
            f"this session cannot reach it. Nothing was run — reconnect the "
            f"cluster to continue. Commands were NOT run against a different "
            f"cluster."
        )


def resolve(session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return the session's cluster connection, or None if it has none.

    Raises ClusterConnectionUnavailable when a connection is recorded but its
    kubeconfig has gone missing.
    """
    if not session_id:
        return None

    try:
        conn = db.get_cluster_connection(session_id)
    except Exception as error:  # a DB hiccup must not silently retarget
        logger.warning("cluster lookup failed for session %s: %s", session_id, error)
        raise ClusterConnectionUnavailable("unknown", f"lookup failed: {error}") from error

    if not conn or not conn.get("context_name"):
        return None

    path = conn.get("kubeconfig_path")
    # An in-cluster or context-only connection carries no file; only validate
    # a path when one was recorded.
    if path and not os.path.isfile(path):
        cluster = conn.get("cluster_name") or conn.get("context_name") or "unknown"
        logger.warning(
            "session %s references a kubeconfig that is gone: %s", session_id, path
        )
        raise ClusterConnectionUnavailable(cluster, "kubeconfig file is missing")

    return conn


def status_for(session_id: str) -> Dict[str, Any]:
    """Connection state for the UI, honest about staleness.

    Returning `connected: false` alone would let the UI show "not connected"
    while the backend still refuses to run anything, which reads as a bug.
    `stale` lets it say why.
    """
    try:
        conn = resolve(session_id)
    except ClusterConnectionUnavailable as error:
        return {
            "connected": False,
            "stale": True,
            "reason": str(error),
            "cluster_name": error.cluster,
        }

    if conn:
        return {
            "connected": True,
            "stale": False,
            "mode": conn["mode"],
            "context_name": conn["context_name"],
            "cluster_name": conn["cluster_name"],
            "server_url": conn["server_url"],
            "namespace": conn["namespace"],
        }

    ssh = db.get_ssh_target(session_id)
    if ssh:
        return {
            "connected": True,
            "stale": False,
            "mode": "ssh",
            "cluster_name": ssh["host"],
            "context_name": f"{ssh['username']}@{ssh['host']}",
            "server_url": "",
            "namespace": "",
        }

    return {"connected": False, "stale": False}
