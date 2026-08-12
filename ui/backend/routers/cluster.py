"""Cluster connection management endpoints.

Supports local kubeconfig autodetection and session-scoped kubeconfig upload.
Selected contexts are persisted by session so chat and execute can target the
same cluster without changing the host's default kubectl context.
"""

import logging
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, Request
from pydantic import BaseModel

import audit
import auth
import cluster_session
import db

from http_errors import safe_error_text

logger = logging.getLogger(__name__)
router = APIRouter()

class UnsafeKubeconfigDir(RuntimeError):
    """The directory kubeconfigs would be written to cannot be trusted."""


def _audit_actor(request) -> str:
    """Who to attribute an event to.

    With auth off — desktop mode and local dev — there is no user to name, and
    refusing to record the event would lose the more important fact that it
    happened at all.
    """
    try:
        user = auth.require_current_user(request)
    except Exception:
        return "local"
    return str((user or {}).get("email") or (user or {}).get("id") or "local")


def _kubeconfig_dir_path() -> Path:
    """Where pasted kubeconfigs live. Desktop overrides this to its app-data dir."""
    configured = os.environ.get("KUBEASTRA_KUBECONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    # The uid is in the name so two accounts on one host do not contend for a
    # single predictable path. Without it, whichever user starts first owns the
    # directory and every other user fails the ownership check below — a fix
    # for one problem that hands you a denial of service instead.
    return Path(tempfile.gettempdir()) / f"kubeastra-kubeconfigs-{os.geteuid()}"


def _ensure_private_dir(path: Path) -> Path:
    """Create the directory 0700, or prove an existing one is safe to use.

    The old code was ``mkdir(parents=True, exist_ok=True)`` followed by a
    ``chmod(0o700)`` wrapped in ``except OSError: pass``. On a multi-user host
    every part of that fails open:

    * The path under ``/tmp`` is predictable, so a local user can create it
      first — as a symlink to a directory they own.
    * ``exist_ok=True`` accepts what it finds and reports nothing.
    * ``chmod`` then fails because the directory is not ours, and the bare
      ``except`` discards the only evidence.
    * ``_write_temp_kubeconfig``'s ``path.resolve().parent != _TEMP_DIR.resolve()``
      guard does not catch it either: both sides resolve *through* the same
      symlink and compare equal.

    The result was that uploaded kubeconfigs — cluster credentials — were
    written into a directory an attacker controlled, silently.

    So: create it exclusively, and if it already exists, verify with ``lstat``
    (a symlink is the attack, and ``stat`` would follow it) that it is a real
    directory, owned by this process, with nothing granted to group or other.
    Anything else raises. There is no safe way to continue, and continuing is
    what caused the problem.
    """
    try:
        os.mkdir(path, 0o700)
        return path
    except FileExistsError:
        pass
    except OSError as exc:
        raise UnsafeKubeconfigDir(
            f"cannot create the kubeconfig directory {path}: {exc.strerror}"
        ) from exc

    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafeKubeconfigDir(
            f"{path} exists but is not a directory — refusing to write "
            "kubeconfigs through it. If this is a symlink, remove it."
        )
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise UnsafeKubeconfigDir(
            f"{path} is owned by uid {info.st_uid}, not this process "
            f"(uid {os.geteuid()}). Refusing to write cluster credentials into "
            "a directory another account controls. Remove it, or set "
            "KUBEASTRA_KUBECONFIG_DIR to a directory you own."
        )
    if info.st_mode & 0o077:
        # Ours, merely loose. Tightening is safe and the common case after an
        # upgrade from the version that created these 0755 under a bad umask.
        os.chmod(path, 0o700)
        logger.warning("tightened permissions on %s to 0700", path)
    return path


# Resolved once at import. A failure here is fatal on purpose: the alternative
# is a server that accepts kubeconfig uploads and quietly puts them somewhere
# unsafe.
_TEMP_DIR = _ensure_private_dir(_kubeconfig_dir_path())


class KubeconfigBody(BaseModel):
    content: str
    session_id: str


class ContextSelectBody(BaseModel):
    session_id: str
    context_name: str
    mode: str = "kubeconfig-upload"
    kubeconfig_path: Optional[str] = None


class DisconnectBody(BaseModel):
    session_id: str


class KubeContext(BaseModel):
    name: str
    cluster: str
    server: str
    user: str
    namespace: str


def _parse_kubeconfig(content: str) -> list[KubeContext]:
    try:
        config = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML: {e}") from e

    if not isinstance(config, dict) or "contexts" not in config:
        raise ValueError("Not a valid kubeconfig file (missing 'contexts' key)")

    clusters = {
        cluster.get("name", ""): (cluster.get("cluster", {}) or {}).get("server", "")
        for cluster in config.get("clusters", [])
    }
    contexts = []
    for ctx in config.get("contexts", []):
        ctx_name = ctx.get("name", "")
        ctx_data = ctx.get("context", {}) or {}
        cluster_name = ctx_data.get("cluster", "")
        contexts.append(KubeContext(
            name=ctx_name,
            cluster=cluster_name,
            server=clusters.get(cluster_name, ""),
            user=ctx_data.get("user", ""),
            namespace=ctx_data.get("namespace", "default"),
        ))
    return contexts


def _sanitize_session_id(session_id: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9\-]", "", session_id)
    if not sanitized:
        raise ValueError("Invalid session ID")
    return sanitized[:64]


def _write_temp_kubeconfig(session_id: str, content: str) -> str:
    safe_id = _sanitize_session_id(session_id)
    path = _TEMP_DIR / f"kubeastra-{safe_id}.yaml"
    if path.resolve().parent != _TEMP_DIR.resolve():
        raise ValueError("Invalid session path")

    # `write_text` then `chmod` created the file at the process umask and
    # narrowed it a moment later — on a 022 umask that is a world-readable
    # window over a cluster credential. O_CREAT with an explicit mode gives it
    # 0600 from birth; O_NOFOLLOW refuses to write through a symlink that
    # appeared where the file should be.
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        stream = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        # fdopen takes ownership of the descriptor only when it succeeds, so
        # this is the one path where we still have to close it ourselves.
        os.close(fd)
        raise
    with stream:
        stream.write(content)
    # O_CREAT only applies the mode when the file is new, so an existing file
    # from an older build keeps whatever it had until this runs.
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    logger.info("Wrote temp kubeconfig for session %s", safe_id)
    return str(path)


def _delete_temp_kubeconfig(path: Optional[str]) -> None:
    if not path:
        return
    try:
        candidate = Path(path)
        if candidate.exists() and candidate.resolve().parent == _TEMP_DIR.resolve():
            candidate.unlink()
            logger.info("Deleted temp kubeconfig %s", candidate.name)
    except Exception as e:
        logger.warning("Failed to delete temp kubeconfig: %s", e)


def _connectivity_check(kubeconfig_path: Optional[str] = None, context: Optional[str] = None) -> dict:
    # Resolved, not bare: this is the first kubectl a user hits, so a GUI
    # launch with a minimal PATH failed here before anything else.
    from k8s import binaries

    cmd = [binaries.kubectl(), "cluster-info"]
    if kubeconfig_path:
        cmd.extend(["--kubeconfig", kubeconfig_path])
    if context:
        cmd.extend(["--context", context])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Connection timed out after 10 seconds"}
    except Exception:
        return {"ok": False, "error": safe_error_text(context="connectivity check")}

    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip()[:500]}

    server_url = ""
    lines = result.stdout.strip().splitlines()
    for line in lines:
        if "control plane" in line.lower() or "master" in line.lower():
            for part in line.split():
                cleaned = re.sub(r"\x1b\[[0-9;]*m", "", part).strip()
                if cleaned.startswith("http"):
                    server_url = cleaned
                    break
    return {"ok": True, "server_url": server_url, "output": lines[0] if lines else ""}


def _get_local_kubeconfig_path() -> Optional[str]:
    env_path = os.environ.get("KUBECONFIG")
    if env_path:
        for path in env_path.split(os.pathsep):
            if Path(path).is_file():
                return path
    default = Path.home() / ".kube" / "config"
    return str(default) if default.is_file() else None


def _allowed_kubeconfig_path(candidate: Optional[str]) -> Optional[str]:
    """Return ``candidate`` only if it is a kubeconfig this server chose to expose.

    ``kubeconfig_path`` arrives in the request body, and every use of it — the
    ``--kubeconfig`` flag, the ``read_text()`` that parses contexts — took it on
    trust. An authenticated user could therefore name any path on the backend
    host and have it opened and parsed as YAML. In desktop mode that is the
    operator's own machine and no escalation; in server mode it is a read
    primitive against a shared host, granted to anyone with an account.

    Only two origins are legitimate: a file this process wrote into _TEMP_DIR
    from pasted content, or the kubeconfig the server itself resolved. Anything
    else is refused rather than sanitised — there is no reason for a third
    value to exist, so accepting one would only widen what has to be reasoned
    about later.
    """
    if not candidate:
        return None
    try:
        resolved = Path(candidate).expanduser().resolve()
    except (OSError, RuntimeError):
        return None

    if resolved.parent == _TEMP_DIR.resolve():
        return str(resolved) if resolved.is_file() else None

    server_choice = _get_local_kubeconfig_path()
    if server_choice and resolved == Path(server_choice).expanduser().resolve():
        return str(resolved)

    logger.warning("refused kubeconfig path outside the allowed locations")
    return None


def _is_in_cluster() -> bool:
    return Path("/var/run/secrets/kubernetes.io/serviceaccount/token").exists()


# No atexit wipe. This used to delete every kubeastra-*.yaml on process exit,
# which was harmless while they lived in /tmp — but desktop mode points
# KUBEASTRA_KUBECONFIG_DIR at the durable app-data directory, so quitting the
# app destroyed every kubeconfig the operator had uploaded while leaving the
# SQLite rows that reference them. The next launch then either ran silently
# against the local cluster, or (once targeting fails closed) refused to run
# at all until they re-uploaded.
#
# Orphans are pruned at startup instead — see cluster_session.prune_orphan_kubeconfigs.


@router.get("/cluster/autodetect")
def autodetect():
    if _is_in_cluster():
        return {
            "in_cluster": True,
            "contexts": [],
            "message": "Running in-cluster with mounted ServiceAccount.",
        }

    kubeconfig_path = _get_local_kubeconfig_path()
    if not kubeconfig_path:
        return {
            "in_cluster": False,
            "contexts": [],
            "kubeconfig_path": None,
            "message": "No kubeconfig found. Upload one or use SSH.",
        }

    try:
        content = Path(kubeconfig_path).read_text()
        contexts = _parse_kubeconfig(content)
        config = yaml.safe_load(content) or {}
        return {
            "in_cluster": False,
            "contexts": [context.model_dump() for context in contexts],
            "kubeconfig_path": kubeconfig_path,
            "current_context": config.get("current-context"),
            "message": f"Found {len(contexts)} context(s) in {kubeconfig_path}",
        }
    except Exception:
        return {
            "in_cluster": False,
            "contexts": [],
            "kubeconfig_path": kubeconfig_path,
            "error": safe_error_text(context="kubeconfig autodetect"),
            "message": "Could not read or parse the kubeconfig on this host.",
        }


@router.post("/cluster/connect/kubeconfig")
def upload_kubeconfig(body: KubeconfigBody, request: Request):
    auth.require_owned_session(request, body.session_id)
    try:
        contexts = _parse_kubeconfig(body.content)
    except ValueError as e:
        return {"error": str(e), "contexts": []}

    if not contexts:
        return {"error": "No contexts found in kubeconfig", "contexts": []}

    temp_path = _write_temp_kubeconfig(body.session_id, body.content)
    config = yaml.safe_load(body.content) or {}
    return {
        "contexts": [context.model_dump() for context in contexts],
        "kubeconfig_path": temp_path,
        "current_context": config.get("current-context"),
        "message": f"Parsed {len(contexts)} context(s). Select one to connect.",
    }


@router.post("/cluster/connect/context")
def connect_context(body: ContextSelectBody, request: Request):
    auth.require_owned_session(request, body.session_id)
    if body.mode == "autodetect" and not body.kubeconfig_path:
        kubeconfig_path = _get_local_kubeconfig_path()
    else:
        # Body-supplied, so it must be one this server put there.
        kubeconfig_path = _allowed_kubeconfig_path(body.kubeconfig_path)
        if body.kubeconfig_path and not kubeconfig_path:
            return {
                "connected": False,
                "error": (
                    "That kubeconfig path is not one this server manages. Upload "
                    "the kubeconfig, or connect with autodetect."
                ),
            }

    check = _connectivity_check(kubeconfig_path, body.context_name)
    if not check["ok"]:
        return {"connected": False, "error": check.get("error", "Connection failed")}

    cluster_name = body.context_name
    namespace = "default"
    if kubeconfig_path:
        try:
            config = yaml.safe_load(Path(kubeconfig_path).read_text()) or {}
            for ctx in config.get("contexts", []):
                if ctx.get("name") == body.context_name:
                    ctx_data = ctx.get("context", {}) or {}
                    cluster_name = ctx_data.get("cluster", body.context_name)
                    namespace = ctx_data.get("namespace", "default")
                    break
        except Exception:
            pass

    db.save_cluster_connection(
        session_id=body.session_id,
        mode=body.mode,
        context_name=body.context_name,
        cluster_name=cluster_name,
        server_url=check.get("server_url", ""),
        namespace=namespace,
        kubeconfig_path=kubeconfig_path if body.mode == "kubeconfig-upload" else None,
    )
    # Choosing a cluster here is also choosing it for background work. An
    # alert has no session to inherit from, and the alternative — defaulting
    # to the machine's current-context — is how proactive investigations
    # ended up pointed at whatever cluster the laptop happened to have.
    cluster_session.remember_default(body.context_name, kubeconfig_path)
    # Which cluster this session can now act on, and who pointed it there.
    # Every mutation later in the trail is only meaningful against this.
    audit.emit(
        audit.EventType.CLUSTER_CONNECTED,
        actor_type="user",
        actor_id=_audit_actor(request),
        session_id=body.session_id,
        cluster=cluster_name,
        subject=body.context_name,
        payload={"mode": body.mode, "namespace": namespace,
                 "server_url": check.get("server_url", "")},
    )
    return {
        "connected": True,
        "cluster_name": cluster_name,
        "context_name": body.context_name,
        "server_url": check.get("server_url", ""),
        "namespace": namespace,
        "mode": body.mode,
    }


@router.post("/cluster/disconnect")
def disconnect(body: DisconnectBody, request: Request):
    auth.require_owned_session(request, body.session_id)
    kubeconfig_path = db.delete_cluster_connection(body.session_id)
    _delete_temp_kubeconfig(kubeconfig_path)
    audit.emit(
        audit.EventType.CLUSTER_DISCONNECTED,
        actor_type="user",
        actor_id=_audit_actor(request),
        session_id=body.session_id,
    )
    return {"disconnected": True}


@router.get("/cluster/status/{session_id}")
def connection_status(session_id: str, request: Request):
    auth.require_owned_session(request, session_id)
    # Delegates to cluster_session so the badge and the code that actually
    # targets kubectl agree on what "connected" means — including the stale
    # case, where a row exists but its kubeconfig has gone.
    return cluster_session.status_for(session_id)
