"""Desktop-mode entry point for the KubeAstra backend.

`main.py` exposes an ASGI `app` but has no `__main__` block — in server mode
uvicorn is invoked externally (docker-compose, Helm). Desktop mode needs a
real entry point, because both the `kubeastra open` launcher and (later) the
Tauri sidecar spawn this as a child process.

Contract with the parent process (stdout, line-oriented):

    PORT=<n>        first line, as soon as the socket is bound
    READY           after the ASGI app has started

The parent parses PORT and polls `http://127.0.0.1:<n>/health`.

Binding happens *here*, before uvicorn starts, so that asking the OS for an
ephemeral port (`--port 0`) is race-free: we hand uvicorn the already-bound
socket rather than announcing a port someone else could grab in between.

Run directly for development:

    python ui/backend/desktop_main.py
"""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

if getattr(sys, "frozen", False):
    _meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    BACKEND_DIR = _meipass
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    _mcp_dir = _meipass / "mcp"
    if _mcp_dir.exists() and str(_mcp_dir) not in sys.path:
        sys.path.insert(0, str(_mcp_dir))
else:
    BACKEND_DIR = Path(__file__).resolve().parent
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))


def announce(line: str) -> None:
    """Write a handshake line to stdout, surviving a closed pipe.

    The parent (the Tauri shell, or `kubeastra open`) reads these lines. If it
    ever stops reading — a bounded buffer, or a reader that returns after
    parsing the handshake — the pipe's read end closes and the next write
    raises BrokenPipeError, killing an otherwise healthy backend.

    A GUI app must not die because nobody is listening to its stdout. Found by
    the Tauri spike: the Rust side stopped reading after the handshake and the
    backend crashed on `READY`.
    """
    try:
        print(line, flush=True)
    except (BrokenPipeError, ValueError):
        # ValueError covers "I/O operation on closed file".
        pass


def watch_parent(poll_seconds: float = 2.0) -> None:
    """Exit when the process that launched us goes away.

    The Tauri shell kills the sidecar on a graceful quit, but that handler
    does not run when the shell is force-quit, crashes, or is signalled — the
    spike confirmed a backend surviving its parent. An orphan keeps the port
    and, worse, the exclusive lock on the vector store, so the next launch
    cannot start.

    On POSIX an orphan is reparented to init/launchd, so a changed ppid is a
    reliable death signal. Skipped when we are already an orphan at startup
    (nohup, a daemonised run, or a developer running this directly) — there
    is no parent to outlive in that case.
    """
    if os.name == "nt":  # pragma: no cover — Windows needs a job object
        return

    original_ppid = os.getppid()
    if original_ppid <= 1:
        return

    def _watch() -> None:
        while True:
            time.sleep(poll_seconds)
            if os.getppid() != original_ppid:
                announce("PARENT-GONE")
                # Hard exit: uvicorn's graceful shutdown can block on open
                # connections, and there is no longer anyone to serve.
                os._exit(0)

    thread = threading.Thread(target=_watch, name="parent-watchdog", daemon=True)
    thread.start()


def bind_socket(host: str, port: int) -> tuple[socket.socket, int]:
    """Bind and return (socket, actual_port). port=0 asks the OS to choose."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    return sock, sock.getsockname()[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="kubeastra-backend")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("KUBEASTRA_DESKTOP_PORT") or 0),
        help="TCP port; 0 (default) asks the OS for a free one.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address. Loopback only — do not expose desktop mode.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("KUBEASTRA_LOG_LEVEL", "warning"),
        help="uvicorn log level (default: warning, to keep stdout clean).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"refusing to bind desktop mode to {args.host!r}: loopback only",
            file=sys.stderr,
        )
        return 2

    # The token must exist before the app imports desktop_security, and the
    # port before the middleware computes its cookie name / allowed origins.
    os.environ["KUBEASTRA_MODE"] = "desktop"
    os.environ.setdefault("KUBEASTRA_DESKTOP_TOKEN", secrets.token_urlsafe(32))

    # Data locations must be set before `main` is imported: settings and
    # db.py read their paths at import time. setdefault, not assignment — an
    # explicit env var from the shell or a developer still wins.
    import desktop_paths

    desktop_paths.ensure_layout()
    os.environ.setdefault("DB_PATH", str(desktop_paths.db_path()))
    os.environ.setdefault("AUDIT_LOG_PATH", str(desktop_paths.audit_log_path()))
    os.environ.setdefault("KUBEASTRA_KUBECONFIG_DIR", str(desktop_paths.kubeconfig_dir()))
    # Investigation memory: embedded vectors, no Qdrant server, embeddings via
    # the user's provider API (no torch in the desktop bundle).
    os.environ.setdefault("VECTOR_DB_MODE", "local")
    os.environ.setdefault("VECTOR_DB_PATH", str(desktop_paths.vectors_path()))
    os.environ.setdefault("EMBEDDINGS_MODE", "api")
    # ALLOWED_NAMESPACES defaults to "default", which is a multi-tenant server
    # guardrail: it stops one team's operator reaching another team's
    # namespaces. On a laptop there is one tenant — the user — and the
    # kubeconfig they chose already bounds what is reachable. Leaving the
    # server default in place silently confines the app to `default`, which no
    # real cluster keeps its workloads in; a kubeastra://investigate link for
    # any other namespace fails with "not in the allowed list".
    # Mutations remain gated by the approval flow, which is the actual
    # safety boundary here. Overridable, for anyone who wants it narrower.
    os.environ.setdefault("ALLOWED_NAMESPACES", "*")

    # Credentials live in the keychain; everything that consumes them reads
    # the environment. Bridge the two here — before `main` is imported, since
    # settings are read and memoised at import time. Without this the app
    # starts with no API key, the LLM provider reports itself disabled, and
    # chat quietly degrades to single-shot tool output with no reasoning
    # trace and no synthesis.
    try:
        import desktop_secrets

        restored = desktop_secrets.restore_to_environ()
        if restored:
            announce(f"LLM_PROVIDER={restored}")
        else:
            announce("LLM_PROVIDER=none (run setup)")
    except Exception as error:  # a broken keychain must not block startup
        print(f"warning: could not restore stored credentials: {error}", file=sys.stderr)

    sock, port = bind_socket(args.host, args.port)
    os.environ["KUBEASTRA_DESKTOP_PORT"] = str(port)

    # Import only after the environment is set: main.py reads KUBEASTRA_MODE
    # at import time to decide on middleware and static mounting.
    import uvicorn  # noqa: E402  (deferred on purpose)

    import main as backend_main  # noqa: E402

    token = os.environ["KUBEASTRA_DESKTOP_TOKEN"]
    announce(f"PORT={port}")
    announce(f"URL=http://127.0.0.1:{port}/auth?token={token}")

    config = uvicorn.Config(
        backend_main.app,
        log_level=args.log_level,
        access_log=False,
        # Streaming (SSE) must not be buffered by a proxy layer; uvicorn
        # writes through, so nothing extra is needed here.
    )
    server = uvicorn.Server(config)

    # Only starts polling if the user has configured and enabled it; the
    # thread otherwise idles. Started after the app is built so a failure here
    # cannot stop the window from opening.
    try:
        import desktop_alerts

        desktop_alerts.poller.start()
    except Exception as error:  # pragma: no cover — never block startup
        print(f"alert polling unavailable: {error}", file=sys.stderr)

    announce("READY")
    # Started after the handshake so a parent that dies mid-startup still gets
    # its PORT line, and before serving so no orphan can outlive the shell.
    watch_parent()
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
