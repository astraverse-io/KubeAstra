"""Cluster connection management endpoints.

Supports local kubeconfig autodetection and session-scoped kubeconfig upload.
Selected contexts are persisted by session so chat and execute can target the
same cluster without changing the host's default kubectl context.
"""

import atexit
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

import auth
import db

logger = logging.getLogger(__name__)
router = APIRouter()

# Where kubeconfigs pasted into the UI are written. Desktop mode points this
# at the per-user app-data directory (0700); the shared-/tmp default is a
# predictable path and is unsafe on multi-user hosts.
_TEMP_DIR = Path(
    os.environ.get("KUBEASTRA_KUBECONFIG_DIR")
    or Path(tempfile.gettempdir()) / "kubeastra-kubeconfigs"
).expanduser()
_TEMP_DIR.mkdir(parents=True, exist_ok=True)
try:
    _TEMP_DIR.chmod(0o700)
except OSError:
    pass


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
    path.write_text(content)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
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
    cmd = ["kubectl", "cluster-info"]
    if kubeconfig_path:
        cmd.extend(["--kubeconfig", kubeconfig_path])
    if context:
        cmd.extend(["--context", context])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Connection timed out after 10 seconds"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

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


def _is_in_cluster() -> bool:
    return Path("/var/run/secrets/kubernetes.io/serviceaccount/token").exists()


def _cleanup_temp_files() -> None:
    try:
        for path in _TEMP_DIR.glob("kubeastra-*.yaml"):
            path.unlink()
    except Exception as e:
        logger.warning("Cluster temp cleanup failed: %s", e)


atexit.register(_cleanup_temp_files)


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
    except Exception as e:
        return {
            "in_cluster": False,
            "contexts": [],
            "kubeconfig_path": kubeconfig_path,
            "error": str(e),
            "message": f"Failed to parse kubeconfig: {e}",
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
    kubeconfig_path = body.kubeconfig_path
    if body.mode == "autodetect" and not kubeconfig_path:
        kubeconfig_path = _get_local_kubeconfig_path()

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
    return {"disconnected": True}


@router.get("/cluster/status/{session_id}")
def connection_status(session_id: str, request: Request):
    auth.require_owned_session(request, session_id)
    conn = db.get_cluster_connection(session_id)
    if conn:
        return {
            "connected": True,
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
            "mode": "ssh",
            "cluster_name": ssh["host"],
            "context_name": f"{ssh['username']}@{ssh['host']}",
            "server_url": "",
            "namespace": "",
        }

    return {"connected": False}
