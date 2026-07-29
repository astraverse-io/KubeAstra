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
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


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

    sock, port = bind_socket(args.host, args.port)
    os.environ["KUBEASTRA_DESKTOP_PORT"] = str(port)

    # Import only after the environment is set: main.py reads KUBEASTRA_MODE
    # at import time to decide on middleware and static mounting.
    import uvicorn  # noqa: E402  (deferred on purpose)

    import main as backend_main  # noqa: E402

    token = os.environ["KUBEASTRA_DESKTOP_TOKEN"]
    print(f"PORT={port}", flush=True)
    print(f"URL=http://127.0.0.1:{port}/auth?token={token}", flush=True)

    config = uvicorn.Config(
        backend_main.app,
        log_level=args.log_level,
        access_log=False,
        # Streaming (SSE) must not be buffered by a proxy layer; uvicorn
        # writes through, so nothing extra is needed here.
    )
    server = uvicorn.Server(config)

    print("READY", flush=True)
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
