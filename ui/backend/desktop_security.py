"""Security boundary for desktop mode (KUBEASTRA_MODE=desktop).

In server mode the app is protected by `auth.auth_boundary_middleware`, which
deliberately exempts machine-to-machine endpoints from user auth (see
`auth.is_public_path`): the Alertmanager webhook, the Prometheus scrape, the
agent invoke route. On a cluster those exemptions sit behind a NetworkPolicy.
On a laptop they would be open doors: any web page the user visits can issue
`fetch("http://127.0.0.1:<port>/api/...")` and drive kubectl against their
production cluster.

This module installs a *default-deny* boundary in front of everything:

  1. Host allowlist       — blocks DNS-rebinding (attacker DNS -> 127.0.0.1).
  2. Origin exact-match   — blocks browser-driven CSRF. Unlike cookies, the
                            Origin header includes the port, so it is the only
                            reliable way to tell "our page" from "some other
                            local server's page".
  3. Per-launch token     — required on every request except a small public
                            allowlist (static assets, health, /auth).

Non-browser callers (curl, local scripts) send no Origin and are allowed
through the Origin check; they still need the token. That is deliberate — an
attacker who can already run code as this user can read the kubeconfig
directly, so the threat model here is specifically the browser.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

logger = logging.getLogger(__name__)

ENV_TOKEN = "KUBEASTRA_DESKTOP_TOKEN"
ENV_PORT = "KUBEASTRA_DESKTOP_PORT"

# Loopback only. IPv6 literals arrive with brackets stripped by the Host parse.
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Paths reachable before the auth cookie exists. Deliberately tiny: the
# pre-auth page load, the readiness probe the launcher polls, and the token
# exchange itself.
_PUBLIC_EXACT = frozenset({"/", "/auth", "/health", "/api/health"})
_PUBLIC_PREFIXES = ("/_next/", "/static/", "/favicon", "/health/")
_STATIC_SUFFIXES = (
    ".html", ".js", ".mjs", ".css", ".map", ".json", ".txt", ".ico",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
)

_token: Optional[str] = None

# First path segments served by the static export (derived at install time
# from the real directory listing, so it can never drift from what shipped).
_static_segments: frozenset[str] = frozenset()

# Never treat these as static even if a same-named directory appears in the
# export: they are API surface and must stay behind the token.
_RESERVED_SEGMENTS = frozenset({"api", "metrics", "auth"})


def get_token() -> str:
    """The per-launch token. Supplied by the launcher/shell, else generated.

    Generated tokens only make sense for a process that also tells someone
    what it generated — `desktop_main` prints the /auth URL for exactly that
    reason.
    """
    global _token
    if _token is None:
        _token = os.environ.get(ENV_TOKEN) or secrets.token_urlsafe(32)
    return _token


def get_port() -> Optional[int]:
    raw = os.environ.get(ENV_PORT)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def cookie_name() -> str:
    """Port-suffixed so two KubeAstra instances don't stomp each other.

    Cookies are NOT port-scoped by the browser, so this only avoids
    self-collision; cross-port leakage is handled by the Origin check.
    """
    port = get_port()
    return f"kubeastra_desktop_{port}" if port else "kubeastra_desktop"


def allowed_origins() -> set[str]:
    port = get_port()
    if not port:
        return set()
    return {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
    }


def is_public_path(path: str) -> bool:
    """True for inert assets and the endpoints needed before the cookie exists.

    The static export is public on purpose: it is the same open-source HTML/JS
    anyone can build from the repo, it carries no data, and the browser must
    fetch it *before* it can present a token. Everything that reads cluster
    state or runs kubectl is router-registered and stays behind the token.
    """
    if path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIXES):
        return True
    if path.endswith(_STATIC_SUFFIXES):
        return True
    segment = path.lstrip("/").split("/", 1)[0]
    if not segment or segment in _RESERVED_SEGMENTS:
        return False
    return segment in _static_segments


def extract_token(request: Request) -> Optional[str]:
    """Bearer header first (Tauri injects it), then the exchange cookie."""
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.cookies.get(cookie_name())


def _wants_html(request: Request) -> bool:
    return "text/html" in (request.headers.get("accept") or "")


def install(app: FastAPI, static_root: "Optional[Path]" = None) -> None:
    """Attach the desktop guard and the /auth token-exchange endpoint.

    Must be called AFTER every router but BEFORE the static catch-all mount:
    Starlette matches routes in registration order, so a mount at "/" would
    shadow /auth. Middleware ordering is independent of route ordering — this
    still registers the guard after main.py's other middleware, which puts it
    outermost (reverse registration order), so unauthorized requests are
    rejected before anything else runs.

    `static_root` is the built frontend export. Its top-level entries define
    which paths are inert public assets; deriving them from the real directory
    keeps the allowlist honest instead of guessing from path shape.
    """
    global _static_segments

    if static_root is not None and static_root.is_dir():
        _static_segments = frozenset(
            entry.name for entry in static_root.iterdir()
        ) - _RESERVED_SEGMENTS
        logger.debug("desktop: static segments %s", sorted(_static_segments))

    token = get_token()
    origins = allowed_origins()

    @app.get("/auth", include_in_schema=False)
    async def desktop_auth_exchange(request: Request):
        """Trade the launch token for an HttpOnly cookie, then redirect.

        The redirect strips the token from the address bar. The token stays
        valid for the life of the process (it is regenerated every launch) so
        the user can reopen the app URL from the launcher without a restart.

        Lands on /chat/ rather than /. The app's root route redirects to /chat
        with next/navigation's server-side `redirect()`, which a static export
        cannot emit — Next bakes an error-boundary document instead, so `/`
        answers 200 with a page reading "Application error". Sending the user
        one hop further makes the first paint the real UI. build-desktop.mjs
        also replaces out/index.html so `/` itself is no longer that document.
        """
        supplied = request.query_params.get("token") or ""
        if not (supplied and hmac.compare_digest(supplied, token)):
            return JSONResponse({"detail": "Invalid or missing token"}, status_code=401)

        response = RedirectResponse("/chat/", status_code=302)
        response.set_cookie(
            key=cookie_name(),
            value=token,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.middleware("http")
    async def desktop_guard(request: Request, call_next):
        path = request.url.path

        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        if host and host not in ALLOWED_HOSTS:
            logger.warning("desktop: rejected foreign Host header %r", host)
            return JSONResponse({"detail": "Forbidden host"}, status_code=403)

        # Origin is the CSRF defense. Cookies are not port-scoped, so a page
        # served by any other localhost port would otherwise be able to ride
        # our cookie. Absent Origin => non-browser client => token-only.
        origin = request.headers.get("origin")
        if origin and origins and origin not in origins:
            logger.warning("desktop: rejected foreign Origin %r", origin)
            return JSONResponse({"detail": "Forbidden origin"}, status_code=403)

        if not is_public_path(path):
            supplied = extract_token(request)
            if not (supplied and hmac.compare_digest(supplied, token)):
                if _wants_html(request) and request.method in SAFE_METHODS:
                    # A stale bookmark to /chat/ before auth: send them to the
                    # app root rather than dumping JSON in the viewport.
                    return RedirectResponse("/", status_code=302)
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        return await call_next(request)

    logger.info(
        "desktop security installed (port=%s, cookie=%s)", get_port(), cookie_name()
    )
