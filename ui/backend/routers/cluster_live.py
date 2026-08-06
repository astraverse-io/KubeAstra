"""Live cluster summary and topology, for the Mission Control chrome.

Both endpoints are polled every 30 seconds per open tab, so both are cached
server-side and single-flighted per session: ten tabs on one session produce
one `kubectl` call, not ten.

Neither returns an error for the ordinary reasons a cluster has nothing to
say. No cluster connected, or a credential that cannot list pods, are states
the header renders — a 4xx would make the panel look broken when it is
merely uninformed.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import auth
import db
from cluster_summary import ClusterSummaryService, RBACError
from cluster_topology import ClusterTopologyService
from http_errors import internal_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/cluster", tags=["Cluster Live"])

# Module-level, so the cache survives between requests. Each keys by
# session_id, and sessions are per-user, so one user's cluster view is never
# served to another.
_summary = ClusterSummaryService(cache_ttl_s=30)
_topology = ClusterTopologyService(cache_ttl_s=30)


class SummaryResponse(BaseModel):
    cluster: Optional[str] = None
    context: Optional[str] = None
    namespace: Optional[str] = None
    counters: Optional[dict] = None
    generated_at: str = ""
    cache_age_seconds: int = 0
    # "no_cluster" | "insufficient_rbac" | None. The UI branches on this
    # rather than on counters being null, so the two cases can be worded
    # differently — one is "connect a cluster", the other is "ask for access".
    reason: Optional[str] = None


class TopologyResponse(BaseModel):
    nodes: list[dict] = []
    edges: list[dict] = []
    generated_at: str = ""


@router.get("/summary/{session_id}", response_model=SummaryResponse)
async def cluster_summary(session_id: str, request: Request) -> SummaryResponse:
    auth.require_owned_session(request, session_id)

    conn = db.get_cluster_connection(session_id)
    if not conn:
        return SummaryResponse(reason="no_cluster")

    try:
        summary = await _summary.get(session_id, conn)
    except RBACError:
        # Not a failure: the cluster answered, and the answer was no.
        return SummaryResponse(
            cluster=conn.get("cluster_name"),
            context=conn.get("context_name"),
            namespace=conn.get("namespace") or "default",
            reason="insufficient_rbac",
        )
    except Exception:
        raise internal_error(context="cluster_summary")

    return SummaryResponse(**summary.as_dict())


@router.get("/topology/{session_id}", response_model=TopologyResponse)
async def cluster_topology(
    session_id: str,
    request: Request,
    scope: Literal["all", "alerting"] = "alerting",
    depth: int = 1,
) -> TopologyResponse:
    auth.require_owned_session(request, session_id)

    if depth not in (1, 2):
        raise HTTPException(status_code=400, detail="depth must be 1 or 2")

    conn = db.get_cluster_connection(session_id)
    if not conn:
        return TopologyResponse()

    try:
        topology = await _topology.get(session_id, conn, scope=scope, depth=depth)
    except Exception:
        raise internal_error(context="cluster_topology")

    return TopologyResponse(**topology.as_dict())
