"""Running an investigation's kubectl against the cluster it is about.

`cluster_routing` decides *which* cluster. This puts that decision into effect,
by installing an SSH runner for the target on the contextvar every kubectl call
already reads — the same mechanism the chat SSH feature uses, so tools reached
through `run_in_threadpool` land on the right host too, since a thread copies
the context.

Two things here matter more than they look.

**Never fall back.** If the target cannot be reached — missing key, unreachable
host, bad config — this raises. It must not quietly leave the default runner
installed, because that is the original bug wearing a disguise: an
investigation that looks routed, runs against whatever the backend is aimed at,
and produces a confident answer about the wrong machine.

**Always close.** A live runner holds a socket, a paramiko transport thread and
a session on the target's sshd. An investigation that leaks one leaks all
three, and enough of them exhausts the target's MaxSessions and this pod's file
descriptors — a failure that shows up as "SSH stopped working" long after the
investigations that caused it finished. So the context manager owns both the
contextvar reset and the close, in a finally.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)


class ClusterUnreachable(RuntimeError):
    """The target exists in the registry but cannot be connected to.

    Distinct from a routing failure: routing said which cluster, and this is
    the attempt to reach it going wrong. The investigation fails rather than
    silently running somewhere else.
    """


def credential_path_for(cluster: dict, secret_dir: str | None = None) -> str:
    """Where this cluster's private key is mounted.

    The registry stores only a file name. Resolving it here — and refusing
    anything that escapes the directory — keeps a registry row from being able
    to name `../../etc/shadow`, which matters because registry rows are written
    through an API rather than by hand.
    """
    from config.settings import get_settings

    base = Path(secret_dir or get_settings().cluster_ssh_secret_dir).resolve()
    ref = str(cluster.get("credential_ref") or "").strip()
    if not ref:
        raise ClusterUnreachable(
            f"cluster {cluster.get('id')!r} has no credential_ref"
        )

    candidate = (base / ref).resolve()
    if not candidate.is_relative_to(base):
        raise ClusterUnreachable(
            f"credential_ref {ref!r} escapes the credential directory"
        )
    if not candidate.is_file():
        raise ClusterUnreachable(
            f"no credential at {candidate} for cluster {cluster.get('id')!r} — "
            f"check the mounted secret and the registry's credential_ref"
        )
    return str(candidate)


def build_runner(cluster: dict, secret_dir: str | None = None):
    """An SSH runner aimed at this cluster's node."""
    from k8s.ssh_runner import SSHKubectlRunner

    credential_path = credential_path_for(cluster, secret_dir)
    try:
        return SSHKubectlRunner(
            host=str(cluster["ssh_host"]),
            username=str(cluster["ssh_user"]),
            port=int(cluster.get("ssh_port") or 22),
            context=cluster.get("kubectl_context") or None,
            credential_path=credential_path,
        )
    except ClusterUnreachable:
        raise
    except Exception as exc:
        raise ClusterUnreachable(
            f"could not build a runner for cluster {cluster.get('id')!r}: {exc}"
        ) from exc


@contextmanager
def routed_execution(cluster: dict | None, secret_dir: str | None = None):
    """Point every kubectl inside this block at `cluster`.

    A None cluster is single-cluster mode or a manual run: nothing is
    installed, and the default runner applies exactly as before. That path has
    to stay free of side effects, because it is the one every existing
    deployment takes.
    """
    if cluster is None:
        yield None
        return

    from k8s.kubectl_runner import runner_ctx, set_runner

    runner = build_runner(cluster, secret_dir)
    token = set_runner(runner)
    logger.info(
        "investigating against cluster %s (%s)",
        cluster.get("id"),
        cluster.get("ssh_host"),
    )
    try:
        yield runner
    finally:
        # Both, always, and in this order: reset first so nothing else can pick
        # up a runner that is about to be closed. Each is guarded separately —
        # a failure to reset must not skip the close, which is the one that
        # leaks resources on someone else's machine.
        try:
            runner_ctx.reset(token)
        except Exception:  # pragma: no cover — defensive
            logger.exception("failed to reset the runner contextvar")
        try:
            runner.close()
        except Exception:  # pragma: no cover — defensive
            logger.exception("failed to close the SSH runner for %s", cluster.get("id"))


def secret_dir_exists(secret_dir: str | None = None) -> bool:
    """Whether the credential mount is present at all.

    Worth distinguishing from a missing individual key: no directory means the
    deployment was never given the secret, which is a different conversation
    from one cluster's key being wrong.
    """
    from config.settings import get_settings

    return os.path.isdir(secret_dir or get_settings().cluster_ssh_secret_dir)
