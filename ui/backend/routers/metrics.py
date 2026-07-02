"""Prometheus scrape endpoint for the alert subsystem.

Exposes the counters that `alerts.shared.metrics.metrics` accumulates in-process
(investigations_started/completed_total, tool_execution_total,
incident_memory_recall_total, incident_memory_store_failures_total, …) in the
text-based Prometheus exposition format. This endpoint is intentionally
exempt from session auth (see `auth.is_public_path`) so an in-cluster
Prometheus can scrape it without a user session; restrict network reachability
via NetworkPolicy or service-mesh policy if scrapers should be outside the
cluster.
"""

import os
import sys
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/api/v1", tags=["metrics"])


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics_endpoint() -> str:
    # Ensure the MCP package is importable. main.py normally injects this at
    # startup; doing it here keeps the endpoint self-sufficient.
    mcp_path = os.environ.get("MCP_PATH") or str(
        Path(__file__).resolve().parent.parent.parent.parent / "mcp"
    )
    if mcp_path not in sys.path:
        sys.path.insert(0, mcp_path)
    from alerts.shared.metrics import metrics
    return metrics.render_prometheus()
