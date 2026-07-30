"""Process, readiness, and dependency diagnostics.

Liveness is intentionally dependency-free. Readiness covers only required
local initialization/persistence. Kubernetes, AI, and Qdrant are diagnostic
dependencies because useful pasted-error analysis remains available without
them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import db

router = APIRouter()
_initialized = False
_configuration_valid = False

# Short TTL cache for the kubectl diagnostic. Protects the API server (and our
# fork budget) when /health is polled externally; Kubernetes-managed probes
# already point at /health/live and /health/ready, so this only guards the
# detailed endpoint.
_KUBECTL_CACHE_TTL_SECONDS = 5.0
_kubectl_cache_lock = threading.Lock()
_kubectl_refresh_lock = threading.Lock()
_kubectl_cache: tuple[float, tuple[bool, str | None, str, "CheckResult"]] | None = None


class CheckResult(BaseModel):
    status: Literal["ok", "degraded", "failed"]
    duration_ms: float = 0.0
    detail: str | None = None


class HealthResponse(BaseModel):
    # Existing fields remain required for backward compatibility.
    status: Literal["ok", "degraded"]
    kubectl_available: bool
    kubectl_context: str | None
    ai_enabled: bool
    qdrant_url: str
    kubectl_mode: Literal["in_cluster", "kubeconfig", "unavailable"]
    checks: dict[str, CheckResult] = Field(default_factory=dict)


def set_initialization_state(*, initialized: bool, configuration_valid: bool) -> None:
    global _initialized, _configuration_valid
    _initialized = initialized
    _configuration_valid = configuration_valid


def _observe_check(name: str, started: float, ok: bool) -> float:
    duration = time.perf_counter() - started
    try:
        from metrics import health_check_duration_seconds, health_check_failures_total
        health_check_duration_seconds.labels(check=name).observe(duration)
        if not ok:
            health_check_failures_total.labels(check=name).inc()
    except Exception:
        pass
    return round(duration * 1000, 1)


def _kubectl_check() -> tuple[bool, str | None, str, CheckResult]:
    global _kubectl_cache
    now = time.monotonic()
    with _kubectl_cache_lock:
        if _kubectl_cache and now - _kubectl_cache[0] < _KUBECTL_CACHE_TTL_SECONDS:
            return _kubectl_cache[1]

    # Single-flight: serialize concurrent misses so we don't fork N kubectl
    # subprocesses on a thundering herd after TTL expiry. The refresh lock is
    # separate from the cache lock so readers hitting a fresh entry stay fast.
    with _kubectl_refresh_lock:
        with _kubectl_cache_lock:
            if _kubectl_cache and time.monotonic() - _kubectl_cache[0] < _KUBECTL_CACHE_TTL_SECONDS:
                return _kubectl_cache[1]
        result = _kubectl_check_uncached()
        with _kubectl_cache_lock:
            _kubectl_cache = (time.monotonic(), result)
        return result


def _kubectl_check_uncached() -> tuple[bool, str | None, str, CheckResult]:
    started = time.perf_counter()
    # Not shutil.which: a GUI launch has a minimal PATH, so `which` returns
    # None even when kubectl is installed in the usual Docker Desktop /
    # Rancher / Homebrew location. That produced "kubectl binary not found"
    # on a machine that had a perfectly working kubectl.
    from k8s import binaries

    kubectl_bin = binaries.found("kubectl")
    if not kubectl_bin:
        duration = _observe_check("kubectl", started, False)
        return False, None, "unavailable", CheckResult(
            status="failed",
            duration_ms=duration,
            detail="kubectl binary not found on PATH or in any known install location",
        )

    try:
        result = subprocess.run(
            [kubectl_bin, "version", "-o", "json", "--request-timeout=3s"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except subprocess.TimeoutExpired:
        duration = _observe_check("kubectl", started, False)
        return False, None, "unavailable", CheckResult(
            status="failed", duration_ms=duration, detail="Kubernetes API check timed out"
        )
    except Exception as exc:
        duration = _observe_check("kubectl", started, False)
        return False, None, "unavailable", CheckResult(
            status="failed", duration_ms=duration, detail=str(exc)[:240]
        )

    if result.returncode != 0:
        duration = _observe_check("kubectl", started, False)
        detail = (result.stderr or result.stdout or "kubectl version failed").strip()[:240]
        return False, None, "unavailable", CheckResult(
            status="failed", duration_ms=duration, detail=detail
        )

    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        context, mode = "in-cluster", "in_cluster"
    else:
        mode = "kubeconfig"
        context = None
        try:
            context_result = subprocess.run(
                [kubectl_bin, "config", "current-context"],
                capture_output=True,
                text=True,
                timeout=1,
                check=False,
            )
            if context_result.returncode == 0:
                context = context_result.stdout.strip() or None
        except Exception:
            pass

    duration = _observe_check("kubectl", started, True)
    # Report which binary was used. When several are installed — Docker
    # Desktop's and Homebrew's routinely coexist, at different versions —
    # "kubectl works" is not enough to explain what a command actually did.
    return True, context, mode, CheckResult(
        status="ok", duration_ms=duration, detail=f"using {kubectl_bin}"
    )


def _readiness_failures() -> list[str]:
    failures: list[str] = []
    if not _initialized:
        failures.append("initialization")
    if not _configuration_valid:
        failures.append("configuration")
    try:
        with db._conn() as con:
            con.execute("SELECT 1").fetchone()
            required = {"sessions", "messages", "users", "auth_sessions"}
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            present = {row["name"] for row in rows}
            if not required.issubset(present):
                failures.append("database_schema")
    except Exception:
        failures.append("database")
    return failures


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    # Keep this async and dependency-free so it does not consume the shared
    # thread pool used by blocking LLM and kubectl work.
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness():
    failures = _readiness_failures()
    if failures:
        try:
            from metrics import readiness_failures_total
            for reason in failures:
                readiness_failures_total.labels(reason=reason).inc()
        except Exception:
            pass
        return JSONResponse(
            {"status": "not_ready", "failures": failures},
            status_code=503,
        )
    return {"status": "ready"}


def _health_status() -> HealthResponse:
    kubectl_ok, current_context, mode, kubectl_result = _kubectl_check()

    provider = os.environ.get("LLM_PROVIDER", "gemini").lower()
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    ollama_url = os.environ.get("OLLAMA_BASE_URL", "")
    ollama_model = os.environ.get("OLLAMA_MODEL", "")
    qdrant_url = os.environ.get("QDRANT_URL", "http://localhost:6333")
    ai_enabled = bool(ollama_url and ollama_model) if provider == "ollama" else bool(gemini_key)

    audit_path = os.environ.get("AUDIT_LOG_PATH", "/app/data/audit.log")
    audit_parent = os.path.dirname(os.path.abspath(audit_path))
    audit_writable = os.path.isdir(audit_parent) and os.access(audit_parent, os.W_OK)
    readiness_failures = _readiness_failures()

    checks: dict[str, Any] = {
        "kubectl": kubectl_result,
        "ai": CheckResult(
            status="ok" if ai_enabled else "degraded",
            detail=None if ai_enabled else f"{provider} is not configured",
        ),
        "qdrant": CheckResult(
            status="ok" if bool(qdrant_url) else "degraded",
            detail=qdrant_url or "QDRANT_URL is not configured",
        ),
        "audit_log": CheckResult(
            status="ok" if audit_writable else "degraded",
            detail=f"path={audit_path} writable={str(audit_writable).lower()}",
        ),
        "readiness": CheckResult(
            status="ok" if not readiness_failures else "failed",
            detail=None if not readiness_failures else ",".join(readiness_failures),
        ),
    }
    degraded = any(check.status != "ok" for check in checks.values())

    return HealthResponse(
        status="degraded" if degraded else "ok",
        kubectl_available=kubectl_ok,
        kubectl_context=current_context,
        kubectl_mode=mode,
        ai_enabled=ai_enabled,
        qdrant_url=qdrant_url,
        checks=checks,
    )


@router.get("/health", response_model=HealthResponse)
@router.get("/api/health", response_model=HealthResponse)
def health_check():
    return _health_status()
