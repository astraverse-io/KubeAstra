"""Thin Prometheus HTTP client used by the prom_query MCP tool.

Sync (httpx.Client, not AsyncClient) so it slots into the tool_registry's
sync handler pattern. The orchestrator already offloads tool dispatches via
fastapi.concurrency.run_in_threadpool, so blocking is fine here.

Configurable via the PROMETHEUS_URL env var (set by the helm chart). If
unset the client returns a clearly-labeled "unavailable" payload rather
than raising — investigations that include prom_query steps still complete,
they just don't get metrics evidence.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _base_url() -> str | None:
    url = os.environ.get("PROMETHEUS_URL", "").strip()
    return url or None


def _timeout_seconds() -> float:
    try:
        return float(os.environ.get("PROMETHEUS_TIMEOUT_SECONDS", "10"))
    except ValueError:
        return 10.0


def query(promql: str) -> dict[str, Any]:
    """Run an instant PromQL query. Returns a result-shaped dict on success,
    or {"unavailable": True, ...} when Prometheus is not configured / errors.
    Never raises so a single broken metric query can't abort an investigation.
    """
    base = _base_url()
    if not base:
        return {
            "unavailable": True,
            "reason": "PROMETHEUS_URL not configured",
            "query": promql,
        }
    try:
        with httpx.Client(base_url=base, timeout=_timeout_seconds()) as client:
            resp = client.get("/api/v1/query", params={"query": promql})
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        logger.warning(f"prom_query failed (query={promql!r}): {exc}")
        return {
            "unavailable": True,
            "reason": str(exc),
            "query": promql,
        }
    return {"query": promql, **payload}


def query_range(
    promql: str, *, start: str, end: str, step: str
) -> dict[str, Any]:
    """Run a range PromQL query. Same fail-soft semantics as query()."""
    base = _base_url()
    if not base:
        return {
            "unavailable": True,
            "reason": "PROMETHEUS_URL not configured",
            "query": promql,
        }
    try:
        with httpx.Client(base_url=base, timeout=_timeout_seconds()) as client:
            resp = client.get(
                "/api/v1/query_range",
                params={"query": promql, "start": start, "end": end, "step": step},
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        logger.warning(f"prom_query_range failed (query={promql!r}): {exc}")
        return {
            "unavailable": True,
            "reason": str(exc),
            "query": promql,
        }
    return {"query": promql, **payload}
