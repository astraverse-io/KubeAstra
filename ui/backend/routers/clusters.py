"""The cluster registry — which clusters this deployment can investigate.

Admin-gated throughout. Registering a cluster changes where investigations run
and what they touch, and adding a row is enough to redirect every alert
carrying that label.

Registering the *first* cluster is the moment this deployment switches from
single-cluster to multi-cluster behaviour, which is a bigger change than it
looks: alerts without a `cluster` label stop being investigated. That is
deliberate — the alternative is investigating them against an arbitrary target
— but it is why the response says so out loud.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import auth
import db
import log_safety

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/clusters", tags=["clusters"])


class ClusterCreate(BaseModel):
    # Must equal the value alerts carry in their `cluster` label. Routing is
    # exact-match on this; there is no normalisation, because guessing that
    # "prod" and "PROD" are the same thing is how an alert reaches the wrong
    # machine.
    id: str = Field(min_length=1)
    ssh_host: str = Field(min_length=1)
    ssh_user: str = Field(min_length=1)
    # The *name* of a mounted secret, never the key material. Anything secret
    # arriving in this field would be written to SQLite in the clear.
    credential_ref: str = Field(min_length=1)
    display_name: str = ""
    ssh_port: int = Field(default=22, gt=0, lt=65536)
    kubectl_context: str = ""
    status: str = "active"


def _require_admin(request: Request) -> dict:
    user = auth.require_current_user(request)
    # With auth disabled — desktop mode, local dev — there is no user to check
    # a role on, and refusing would make the registry unusable in the mode most
    # people try first.
    if user and not auth.is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@router.get("")
def list_clusters(request: Request, include_disabled: bool = True) -> dict:
    _require_admin(request)
    clusters = db.list_clusters(include_disabled=include_disabled)
    return {
        "clusters": clusters,
        "count": len(clusters),
        # Says which mode the deployment is in, because the same alert behaves
        # differently either side of that line.
        "multi_cluster": not db.registry_is_empty(),
    }


@router.post("", status_code=201)
def create_cluster(request: Request, body: ClusterCreate) -> dict:
    _require_admin(request)
    was_empty = db.registry_is_empty()

    try:
        cluster = db.register_cluster(
            cluster_id=body.id,
            ssh_host=body.ssh_host,
            ssh_user=body.ssh_user,
            credential_ref=body.credential_ref,
            display_name=body.display_name,
            ssh_port=body.ssh_port,
            kubectl_context=body.kubectl_context,
            status=body.status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info("cluster %s registered", log_safety.one_line(body.id))
    return {
        "cluster": cluster,
        # Surfaced rather than buried in docs: this is the call that changes
        # how every unlabelled alert is handled.
        "switched_to_multi_cluster": was_empty,
    }


@router.delete("/{cluster_id}")
def delete_cluster(request: Request, cluster_id: str) -> dict:
    _require_admin(request)
    if not db.remove_cluster(cluster_id):
        raise HTTPException(status_code=404, detail="Cluster not found")

    logger.info("cluster %s removed", log_safety.one_line(cluster_id))
    return {
        "id": cluster_id,
        # Removing the last cluster returns the deployment to single-cluster
        # mode, which starts investigating unlabelled alerts again.
        "back_to_single_cluster": db.registry_is_empty(),
    }
