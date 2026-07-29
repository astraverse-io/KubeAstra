"""FastAPI backend for the KubeAstra UI.

Imports tool functions directly from mcp (via MCP_PATH env var)
so there is zero code duplication.

Run locally:
    MCP_PATH=../../mcp uvicorn main:app --reload --port 8000
"""

import os
import sys
import time
import uuid
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ── Resolve mcp path ───────────────────────────────────────────────
MCP_PATH = os.environ.get(
    "MCP_PATH",
    str(Path(__file__).resolve().parent.parent.parent / "mcp"),
)
if MCP_PATH not in sys.path:
    sys.path.insert(0, MCP_PATH)

# Load .env from MCP project so all settings (GEMINI_API_KEY etc.) are available
from dotenv import load_dotenv
_mcp_env = Path(MCP_PATH) / ".env"
if _mcp_env.exists():
    load_dotenv(str(_mcp_env))

import db
import metrics
import auth as auth_utils
import tracing
from routers import ai_tools, kubectl, recovery, health, chat, sessions, cluster, feedback, models, alerts, agent_runs, admin, agent, metrics as metrics_router
from routers import auth as auth_router

logger = logging.getLogger(__name__)

# ── Run mode ───────────────────────────────────────────────────────
# "server"  — the deployed form (docker-compose / Helm); auth, CORS and the
#             machine-to-machine exemptions in auth.is_public_path apply.
# "desktop" — single-user app on a laptop; desktop_security replaces the auth
#             boundary with a localhost/origin/token guard and the built
#             frontend is served from this same origin.
KUBEASTRA_MODE = os.environ.get("KUBEASTRA_MODE", "server").strip().lower()
DESKTOP_MODE = KUBEASTRA_MODE == "desktop"


def _bootstrap_rag_collections() -> None:
    """Create every known RAG collection up-front so grounded-retrieval
    queries don't 404 on the first chat after a fresh deploy.

    The codepath in services/rag/capture.py only calls ensure_collection_for
    on the WRITE side (when a session is judged worthy of capture). Reads
    happen on every chat via the grounded-retrieval router, well before any
    write — so until the first capture lands, every chat logs:

        Vector search in session_memory failed: 404 Collection doesn't exist

    The 404 is benign (search returns empty), but it pollutes logs and
    would mask a real Qdrant outage. Creating the collections at startup
    converts the 404 into a clean "0 results" log.

    Wrapped in a broad try/except: if Qdrant is unreachable on boot, the
    app must still come up — chats just won't have RAG grounding until
    the next pod restart picks Qdrant back up. RAG is degrade-gracefully,
    not load-bearing for the basic chat flow.
    """
    try:
        from services.vector_db import vector_db
        from services.rag.schema import (
            DEPLOYMENT_REPO, DEVOPS_DOC, RUNBOOK, SESSION_MEMORY,
        )
    except Exception as exc:
        logger.warning("RAG bootstrap: import failed (%s); skipping", exc)
        return

    try:
        vector_db.connect()
    except Exception as exc:
        logger.warning("RAG bootstrap: Qdrant connect failed (%s); skipping", exc)
        return

    # Order matches services/rag/schema.ALL_COLLECTIONS (higher-trust first).
    # K8S_ERROR is excluded — it's seeded by data/seed.py when used and
    # nothing in the live chat path queries it, so don't pay the create cost.
    for spec in (RUNBOOK, DEVOPS_DOC, DEPLOYMENT_REPO, SESSION_MEMORY):
        try:
            vector_db.ensure_collection_for(spec)
        except Exception as exc:
            logger.warning(
                "RAG bootstrap: ensure_collection_for(%s) failed (%s)",
                spec.name, exc,
            )

    logger.info("RAG bootstrap: ensured runbook, devops_doc, deployment_repo, session_memory")

    # Pre-warm the sentence-transformer model. Without this, embeddings.embed()
    # is lazy-loaded on the first chat's first call (~5-10s while the model
    # downloads from HF cache to RAM and loads tokenizer + weights). That
    # delay shows up as a confusing "is the app hung?" pause for whoever
    # asks the first question after a fresh pod. Moving the cost to startup
    # — where pod-boot latency is already absorbed by readiness probes —
    # makes the first chat feel as snappy as every subsequent one.
    #
    # Wrapped in try/except for the same reason as everything else here:
    # warmup is an optimization, not load-bearing. If the model isn't on
    # disk and HF is unreachable, we'd rather start without a warm cache
    # than fail the pod.
    try:
        from services.embeddings import embeddings
        embeddings.embed("warmup")
        logger.info("RAG bootstrap: embedding model warmed")
    except Exception as exc:
        logger.warning("RAG bootstrap: embedding warmup failed (%s)", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    health.set_initialization_state(initialized=False, configuration_valid=False)
    configuration_valid = False
    try:
        from config.settings import get_settings
        get_settings().validate_settings()
        configuration_valid = True
    except Exception:
        logger.exception("Application configuration validation failed")
        raise
    if tracing.init_tracing():
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            FastAPIInstrumentor().instrument_app(app)
            logger.info("FastAPI app auto-instrumented with OpenTelemetry.")
        except Exception as exc:
            logger.error("Failed to instrument FastAPI app: %s", exc)
    db.init_db()
    db.sweep_orphaned_investigations()
    _bootstrap_rag_collections()
    health.set_initialization_state(
        initialized=True,
        configuration_valid=configuration_valid,
    )
    try:
        yield
    finally:
        health.set_initialization_state(
            initialized=False,
            configuration_valid=configuration_valid,
        )


app = FastAPI(
    title="KubeAstra Assistant API",
    description="REST API exposing all 33 mcp tools for team self-service",
    version="1.0.0",
    lifespan=lifespan,
)

_auth_settings = auth_utils.get_auth_settings()

if DESKTOP_MODE:
    # Same-origin app: the only legitimate origin is our own loopback port.
    # The wildcard used in server mode would let any page read our responses.
    import desktop_security

    _cors_origins = sorted(desktop_security.allowed_origins())
    _cors_credentials = True
else:
    _cors_origins = (
        list(_auth_settings.allowed_origins)
        if _auth_settings.enabled and _auth_settings.allowed_origins
        else ["*"]
    )
    _cors_credentials = not _auth_settings.enabled or bool(_auth_settings.allowed_origins)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def auth_boundary_middleware(request: Request, call_next):
    """Require local auth for operational APIs when AUTH_ENABLED=true."""
    if not auth_utils.auth_enabled():
        return await call_next(request)

    path = request.url.path
    if not path.startswith("/api") and path != "/health":
        return await call_next(request)

    if not auth_utils.origin_allowed(request):
        return JSONResponse({"detail": "Origin is not allowed"}, status_code=403)

    if auth_utils.is_public_path(path, request.method.upper()):
        return await call_next(request)

    user = auth_utils.get_current_user_optional(request)
    if not user:
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    request.state.user = user
    return await call_next(request)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    request_id = uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    started_at = time.perf_counter()
    status_code = None

    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.exception(
            "request_id=%s method=%s path=%s status=500 elapsed_ms=%.1f",
            request_id,
            request.method,
            request.url.path,
            elapsed_ms,
        )
        raise
    finally:
        elapsed_sec = time.perf_counter() - started_at
        path = request.url.path
        if path != "/metrics":
            try:
                if path == "/api/chat":
                    route_label = "chat"
                elif path == "/api/chat/stream":
                    route_label = "chat_stream"
                elif path.startswith("/api/admin"):
                    route_label = "admin"
                else:
                    route_label = "other"

                if status_code is None:
                    status_label = "server_error"
                elif status_code < 400:
                    status_label = "success"
                elif status_code < 500:
                    status_label = "client_error"
                else:
                    status_label = "server_error"

                from metrics import chat_request_duration_seconds, chat_requests_total
                chat_request_duration_seconds.labels(route=route_label, status=status_label).observe(elapsed_sec)
                chat_requests_total.labels(route=route_label, status=status_label).inc()
            except Exception:
                pass

    elapsed_ms = elapsed_sec * 1000
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request_id=%s method=%s path=%s status=%s elapsed_ms=%.1f",
        request_id,
        request.method,
        request.url.path,
        status_code or 500,
        elapsed_ms,
    )
    return response

app.include_router(health.router, tags=["health"])
app.include_router(auth_router.router, prefix="/api", tags=["Auth"])
app.include_router(chat.router, prefix="/api", tags=["Chat"])
app.include_router(sessions.router, prefix="/api", tags=["Sessions"])
app.include_router(cluster.router, prefix="/api", tags=["Cluster"])
app.include_router(ai_tools.router, prefix="/api", tags=["AI Analysis"])
app.include_router(kubectl.router, prefix="/api", tags=["Kubectl"])
app.include_router(recovery.router, prefix="/api", tags=["Recovery"])
app.include_router(feedback.router, prefix="/api", tags=["Feedback"])
app.include_router(models.router, prefix="/api", tags=["Models"])
app.include_router(alerts.router, tags=["Alerts"])
app.include_router(metrics_router.router, tags=["Metrics"])
app.include_router(agent_runs.router, prefix="/api", tags=["AgentRuns"])
app.include_router(admin.router, prefix="/api/admin", tags=["admin"])
_agent_router = agent.router_from_environment()
if _agent_router is not None:
    app.include_router(_agent_router)
else:
    logger.warning(
        "No legacy or scoped agent API credentials are configured; "
        "/api/v1/agent/invoke is not registered"
    )


@app.get("/metrics")
def get_metrics(request: Request):
    expected_token = os.environ.get("METRICS_TOKEN")
    if expected_token:
        auth_header = request.headers.get("Authorization")
        x_token = request.headers.get("X-Metrics-Token")

        token = None
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]
        elif x_token:
            token = x_token

        import hmac
        if not token or not hmac.compare_digest(token, expected_token):
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Unauthorized metrics access")

    from fastapi.responses import Response
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── Desktop mode: security boundary + same-origin frontend ─────────
# Registered last on purpose. Starlette runs HTTP middleware in reverse
# registration order, so the desktop guard ends up outermost and rejects
# unauthorized requests before auth/logging middleware ever runs. The static
# mount must also come after every router, or "/" would shadow the API.
if DESKTOP_MODE:
    from typing import Optional

    from fastapi.staticfiles import StaticFiles
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from starlette.responses import Response

    def _resolve_frontend_dist() -> Path:
        override = os.environ.get("KUBEASTRA_FRONTEND_DIST")
        if override:
            return Path(override).expanduser().resolve()
        # Frozen bundles (PyInstaller) place the export next to the binary.
        bundled = Path(getattr(sys, "_MEIPASS", "")) / "frontend" if getattr(sys, "_MEIPASS", "") else None
        if bundled and bundled.is_dir():
            return bundled
        return (Path(__file__).resolve().parent.parent / "frontend" / "out").resolve()

    class _SPAStaticFiles(StaticFiles):
        """Static export server with a directory-index fallback.

        `next build` with trailingSlash emits `/chat/index.html`. A user
        typing `/chat` (no slash) would otherwise 404, so extension-less
        misses retry as `<path>/index.html`.
        """

        async def get_response(self, path: str, scope):
            miss: Optional[Response] = None
            try:
                response = await super().get_response(path, scope)
                if response.status_code != 404:
                    return response
                miss = response
            except StarletteHTTPException as exc:
                if exc.status_code != 404:
                    raise

            leaf = path.rsplit("/", 1)[-1]
            if "." not in leaf:
                try:
                    return await super().get_response(
                        f"{path.rstrip('/')}/index.html", scope
                    )
                except StarletteHTTPException:
                    pass

            if miss is not None:
                return miss
            raise StarletteHTTPException(status_code=404)

    _frontend_dist = _resolve_frontend_dist()

    # install() before mount(): Starlette matches routes in registration
    # order, so a catch-all mount at "/" would shadow /auth.
    desktop_security.install(app, static_root=_frontend_dist)

    if _frontend_dist.is_dir():
        app.mount("/", _SPAStaticFiles(directory=str(_frontend_dist), html=True), name="frontend")
        logger.info("desktop: serving frontend from %s", _frontend_dist)
    else:
        logger.warning(
            "desktop: no frontend export at %s — API only. "
            "Build it with: npm run build:desktop",
            _frontend_dist,
        )
