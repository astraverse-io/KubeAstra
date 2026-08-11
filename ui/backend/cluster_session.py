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
import log_safety

logger = logging.getLogger(__name__)


def unavailable_message(cluster: str) -> str:
    """The text an operator sees when their cluster has gone.

    A function rather than a line inside the exception so that callers can
    build it from `error.cluster` instead of from `str(error)`. Serialising an
    exception into a response is the shape CodeQL's stack-trace-exposure query
    looks for (alert 114), and it is right to look: the day someone raises
    this with an interpolated internal detail, the detail ships to the client.
    Taking a plain string attribute cannot do that whatever the exception says.
    """
    return (
        f"The kubeconfig for cluster '{cluster}' is no longer available, so "
        f"this session cannot reach it. Nothing was run — reconnect the "
        f"cluster to continue. Commands were NOT run against a different "
        f"cluster."
    )


class ClusterConnectionUnavailable(RuntimeError):
    """A session's cluster is configured but no longer reachable.

    Raised instead of falling back, so a command never runs somewhere the
    operator did not choose.
    """

    def __init__(self, cluster: str, reason: str) -> None:
        self.cluster = cluster
        self.reason = reason
        super().__init__(unavailable_message(cluster))


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
        # session_id arrives from the request. A newline in it lets the
        # writer append a line that reads like the application produced it,
        # which is what log_safety.one_line exists to prevent (alert 148).
        logger.warning(
            "cluster lookup failed for session %s: %s",
            log_safety.one_line(session_id),
            log_safety.one_line(error),
        )
        # The underlying error is logged above and chained below; it is
        # deliberately not interpolated into `reason`. Nothing reads .reason —
        # every handler renders str(exc), which is fixed text — so carrying the
        # database message here bought nothing and gave CodeQL a real path from
        # an exception string to an HTTP response (alert 114). The message
        # never actually reached a response, because __init__ builds a fixed
        # string, but the shorter answer is not to hold it at all.
        raise ClusterConnectionUnavailable("unknown", "lookup failed") from error

    if not conn or not conn.get("context_name"):
        return None

    path = conn.get("kubeconfig_path")
    # An in-cluster or context-only connection carries no file; only validate
    # a path when one was recorded.
    if path and not os.path.isfile(path):
        cluster = conn.get("cluster_name") or conn.get("context_name") or "unknown"
        logger.warning(
            "session %s references a kubeconfig that is gone: %s",
            log_safety.one_line(session_id),
            log_safety.one_line(path),
        )
        raise ClusterConnectionUnavailable(cluster, "kubeconfig file is missing")

    return conn


class NoDefaultCluster(RuntimeError):
    """Background work was asked to run with no cluster chosen.

    Distinct from ClusterConnectionUnavailable: nothing is broken, nothing
    has been picked yet.
    """


def _desktop_mode() -> bool:
    return (os.environ.get("KUBEASTRA_MODE") or "").lower() == "desktop"


def resolve_default() -> Optional[Dict[str, Any]]:
    """The cluster background work should target, or None in server mode.

    Chat binds a runner per session. An alert arriving from Alertmanager has
    no session, so `get_runner()` returned the ambient runner — the machine's
    `current-context`. On a laptop that is routinely an employer's cluster
    the operator never chose for this app, and a proactive investigation
    would happily run `kubectl get pods --all-namespaces` against it.

    Server mode is the opposite case: the ambient runner *is* the intended
    cluster, an in-cluster ServiceAccount with a bounded RBAC role. Returning
    None there preserves that.

    Raises NoDefaultCluster in desktop mode when nothing has been chosen, so
    the caller refuses rather than guessing.
    """
    if not _desktop_mode():
        return None

    import desktop_config

    stored = desktop_config.load()
    context = (stored.get("default_cluster_context") or "").strip()
    if not context:
        raise NoDefaultCluster(
            "No cluster has been selected for background investigations. "
            "Connect a cluster in KubeAstra first — nothing was run, and no "
            "command was sent to any cluster."
        )

    path = (stored.get("default_cluster_kubeconfig") or "").strip()
    if path and not os.path.isfile(path):
        raise ClusterConnectionUnavailable(context, "kubeconfig file is missing")

    return {"context_name": context, "kubeconfig_path": path or None}


def remember_default(context_name: str, kubeconfig_path: Optional[str]) -> None:
    """Record the cluster the operator just connected to.

    Called from the connect flow so choosing a cluster in the UI is also the
    act of choosing it for alerts — rather than a second, separate setting
    nobody would find, whose default would have to be *something*.
    """
    if not _desktop_mode() or not context_name:
        return
    try:
        import desktop_config

        desktop_config.save({
            "default_cluster_context": context_name,
            "default_cluster_kubeconfig": kubeconfig_path or "",
        })
        logger.info("background investigations will target %s", context_name)
    except Exception as error:  # never fail a working connect over this
        logger.warning("could not record default cluster: %s", error)


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
            # Built from the cluster name, not from str(error). Identical text,
            # but nothing an exception carries can reach the client through it.
            "reason": unavailable_message(error.cluster),
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


def prune_orphan_kubeconfigs(directory: Optional[str] = None) -> int:
    """Delete uploaded kubeconfigs no session references any more.

    Replaces an `atexit` handler that wiped the whole directory on exit. That
    was harmless while uploads lived in /tmp, but desktop mode stores them in
    the durable app-data directory — so quitting destroyed every kubeconfig
    the operator had uploaded, while the rows referencing them survived.

    Deletes nothing if the referenced set cannot be read: losing a live
    kubeconfig is far worse than leaving a stale file on disk.
    """
    import glob

    if directory is None:
        directory = os.environ.get("KUBEASTRA_KUBECONFIG_DIR") or ""
    if not directory or not os.path.isdir(directory):
        return 0

    try:
        referenced = {
            row["kubeconfig_path"]
            for row in db.list_cluster_connections()
            if row.get("kubeconfig_path")
        }
    except Exception as error:
        logger.warning("kubeconfig prune skipped — cannot read references: %s", error)
        return 0

    removed = 0
    for path in glob.glob(os.path.join(directory, "kubeastra-*.yaml")):
        if path in referenced:
            continue
        try:
            os.unlink(path)
            removed += 1
        except OSError as error:
            logger.warning("could not prune %s: %s", path, error)
    if removed:
        logger.info("pruned %d orphaned kubeconfig(s)", removed)
    return removed
