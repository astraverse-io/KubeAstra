"""`kubeastra open` — run KubeAstra as a local app in the browser.

Phase 0 of the desktop plan: prove the whole desktop UX (local backend, own
kubeconfig, same-origin frontend, token auth) without any packaging. The
Tauri shell added later drives the same backend the same way — it replaces
the browser, not the contract.

Lifecycle:

    spawn `desktop_main.py`  ->  read `PORT=<n>` from stdout
                             ->  poll  http://127.0.0.1:<n>/health
                             ->  open  http://127.0.0.1:<n>/auth?token=...
                             ->  wait; SIGINT / exit stops the child

Single-instance: a lockfile in the state dir records pid+port+token. A second
`kubeastra open` against a live instance opens the browser at the running one
instead of starting a second backend — qdrant-client's local mode takes an
exclusive file lock, so two backends would break investigation memory.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

READY_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 0.2


def state_dir() -> Path:
    """Per-user state location. Mirrors the desktop app-data layout."""
    override = os.environ.get("KUBEASTRA_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "KubeAstra"
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "KubeAstra"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "kubeastra"


def lockfile() -> Path:
    return state_dir() / "desktop.json"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_running_instance() -> Optional[dict]:
    """Return the live instance's record, or None (cleaning up stale files)."""
    path = lockfile()
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return None

    pid = record.get("pid")
    if not isinstance(pid, int) or not _pid_alive(pid):
        path.unlink(missing_ok=True)
        return None
    if not health_ok(record.get("port")):
        path.unlink(missing_ok=True)
        return None
    return record


def write_instance(pid: int, port: int, token: str) -> None:
    path = lockfile()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "port": port, "token": token}))
    try:
        path.chmod(0o600)  # the token is in here
    except OSError:
        pass


def health_ok(port: Optional[int], timeout: float = 1.0) -> bool:
    if not port:
        return False
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=timeout
        ) as response:
            return 200 <= response.status < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


def backend_entrypoint() -> Optional[Path]:
    """Locate desktop_main.py — dev checkout for now.

    Packaged installs will ship the backend as package data or a PyInstaller
    sidecar; that resolution lands in Phase 1 (see DESKTOP_APP_PLAN open
    question 3). Until then this supports running from a source checkout.
    """
    override = os.environ.get("KUBEASTRA_BACKEND_ENTRY")
    if override:
        candidate = Path(override).expanduser().resolve()
        return candidate if candidate.is_file() else None

    # cli/src/kubeastra/desktop.py -> repo root is four parents up.
    repo_root = Path(__file__).resolve().parents[3]
    candidate = repo_root / "ui" / "backend" / "desktop_main.py"
    return candidate if candidate.is_file() else None


def _read_handshake(process: subprocess.Popen, deadline: float) -> tuple[Optional[int], Optional[str]]:
    """Read `PORT=` / `URL=` lines the backend prints once bound."""
    port: Optional[int] = None
    token: Optional[str] = None
    assert process.stdout is not None

    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        line = process.stdout.readline()
        if not line:
            break
        line = line.strip()
        if line.startswith("PORT="):
            try:
                port = int(line[5:])
            except ValueError:
                pass
        elif line.startswith("URL=") and "token=" in line:
            token = line.split("token=", 1)[1]
        if port and token:
            break
    return port, token


def launch(open_browser: bool = True, echo=print) -> int:
    """Start (or attach to) a local KubeAstra and open it in the browser."""
    running = read_running_instance()
    if running:
        url = f"http://127.0.0.1:{running['port']}/auth?token={running['token']}"
        echo(f"KubeAstra is already running on port {running['port']} — opening it.")
        if open_browser:
            webbrowser.open(url)
        return 0

    entry = backend_entrypoint()
    if entry is None:
        echo(
            "Could not find the KubeAstra backend.\n"
            "Run `kubeastra open` from a source checkout, or set "
            "KUBEASTRA_BACKEND_ENTRY to the path of ui/backend/desktop_main.py."
        )
        return 1

    env = dict(os.environ)
    env["KUBEASTRA_MODE"] = "desktop"
    env.setdefault("PYTHONUNBUFFERED", "1")

    echo("Starting KubeAstra…")
    process = subprocess.Popen(
        [sys.executable, str(entry)],
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        env=env,
    )

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    port, token = _read_handshake(process, deadline)

    if not port or not token:
        process.terminate()
        echo("Backend failed to start (no port handshake). Check the output above.")
        return 1

    while time.monotonic() < deadline and not health_ok(port):
        if process.poll() is not None:
            echo("Backend exited during startup.")
            return 1
        time.sleep(POLL_INTERVAL_SECONDS)

    if not health_ok(port):
        process.terminate()
        echo(f"Backend did not become healthy within {READY_TIMEOUT_SECONDS:.0f}s.")
        return 1

    write_instance(process.pid, port, token)
    url = f"http://127.0.0.1:{port}/auth?token={token}"
    echo(f"KubeAstra is running at http://127.0.0.1:{port}")
    echo("Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)

    try:
        process.wait()
    except KeyboardInterrupt:
        echo("\nStopping KubeAstra…")
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    finally:
        lockfile().unlink(missing_ok=True)
    return 0
