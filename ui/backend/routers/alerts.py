import contextlib
import hmac
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

import cluster_session
import db
import auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


@contextlib.contextmanager
def _bound_to_chosen_cluster():
    """Target the cluster the operator chose, or nothing at all.

    An alert has no session to inherit a runner from, so every kubectl call
    below used to fall through `get_runner()` to the ambient runner — the
    machine's `current-context`. On a laptop that is routinely an employer's
    cluster the operator never pointed this app at, and a proactive
    investigation would run `kubectl get pods --all-namespaces` against it.

    In server mode `resolve_default()` returns None and the ambient runner is
    correct: it is the in-cluster ServiceAccount, which is the whole point.
    """
    conn = cluster_session.resolve_default()
    if not conn:
        yield None
        return

    from k8s.kubectl_runner import KubectlRunner, runner_ctx, set_runner

    token = set_runner(KubectlRunner(
        kubeconfig_path=conn.get("kubeconfig_path"),
        context=conn["context_name"],
    ))
    logger.info("investigation bound to cluster %s", conn["context_name"])
    try:
        yield conn
    finally:
        runner_ctx.reset(token)


def _resolve_mcp_path() -> str:
    """Locate the mcp package. Anchor to the MCP_PATH env var (set in
    the container image and at runtime), with a repo-relative fallback for local
    dev. Avoids the brittle '../../../' traversal that breaks once the backend and
    MCP are laid out differently in the image than in the source tree."""
    mcp_path = os.environ.get("MCP_PATH")
    if mcp_path and os.path.isdir(mcp_path):
        return mcp_path
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../../../mcp")
    )


def _resolve_playbook_path() -> str:
    """Bundled playbook directory, anchored to the resolved MCP path."""
    return os.path.join(_resolve_mcp_path(), "data", "playbooks")


def _verify_webhook_token(authorization: str | None) -> None:
    """Authenticate machine-to-machine webhook callers via a shared bearer token.

    The webhook is exempt from interactive user-session auth (it is listed in
    auth.is_public_path so Alertmanager isn't 401'd by the session middleware),
    so this is the security boundary for it. When ALERT_WEBHOOK_TOKEN is set we
    require a matching `Authorization: Bearer <token>`; when it is unset the
    webhook stays open for local/dev use."""
    expected = os.environ.get("ALERT_WEBHOOK_TOKEN")
    if not expected:
        return
    provided = ""
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[len("bearer "):].strip()
    # Compare as bytes so any (even non-ASCII) token value is handled safely.
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid or missing webhook token")

async def orchestrate_investigation(alert: Any, investigation_id: str, repo: db.SqliteInvestigationRepository):
    try:
        # Make the MCP package importable BEFORE importing from `alerts` below
        # (anchor to MCP_PATH; dev fallback). main.py normally does this at
        # startup, but doing it here keeps the background task self-sufficient.
        import sys
        mcp_path = _resolve_mcp_path()
        if mcp_path not in sys.path:
            sys.path.insert(0, mcp_path)

        from alerts.playbooks.registry import PlaybookRegistry
        from alerts.playbooks.loader import PlaybookLoader
        from alerts.playbooks.classifier import AlertClassifier
        from alerts.notifications.dispatcher import NotificationDispatcher, LoggingNotificationChannel
        from alerts.orchestrator.engine import InvestigationOrchestrator

        # Locate bundled playbooks (anchored to MCP_PATH; see _resolve_playbook_path)
        playbook_path = _resolve_playbook_path()

        playbooks = PlaybookRegistry(PlaybookLoader(playbook_path))
        classifier = AlertClassifier(
            playbooks.list(),
            rules_path=os.path.join(playbook_path, "classification-rules.yaml")
        )

        try:
            from services.vector_db import vector_db
            from alerts.repositories.qdrant import QdrantSemanticMemoryRepository
            vector_db.connect()
            semantic_memory = QdrantSemanticMemoryRepository(vector_db, "incident_memory")
        except Exception as e:
            logger.warning(f"Qdrant connection failed; falling back to in-memory semantic repository: {e}")
            from alerts.repositories.in_memory import InMemorySemanticMemoryRepository
            semantic_memory = InMemorySemanticMemoryRepository()

        orchestrator = InvestigationOrchestrator(
            playbooks=playbooks,
            classifier=classifier,
            investigations=repo,
            semantic_memory=semantic_memory,
            notifications=NotificationDispatcher([LoggingNotificationChannel()]),
        )

        with _bound_to_chosen_cluster():
            await orchestrator.investigate(alert, investigation_id)
    except (cluster_session.NoDefaultCluster,
            cluster_session.ClusterConnectionUnavailable) as e:
        # Refuse rather than fall back. Running this against whatever the
        # machine's kubeconfig points at is the failure mode being closed.
        logger.warning("investigation %s not run: %s", investigation_id, e)
        investigation = await repo.get(investigation_id)
        if investigation:
            from alerts.domain.enums import InvestigationStatus
            investigation.status = InvestigationStatus.FAILED
            investigation.append_audit("no_cluster_selected", {"error": str(e)})
            await repo.save(investigation)
        return
    except Exception as e:
        logger.error(f"Background orchestrator failed for {investigation_id}: {e}")
        # update status to failed
        investigation = await repo.get(investigation_id)
        if investigation:
            from alerts.domain.enums import InvestigationStatus
            investigation.status = InvestigationStatus.FAILED
            investigation.append_audit("orchestrator_crashed", {"error": str(e)})
            await repo.save(investigation)


class AlertWebhookResponse(BaseModel):
    investigation_ids: list[str]
    status: str

@router.post("/webhook", response_model=AlertWebhookResponse)
async def receive_webhook(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
) -> AlertWebhookResponse:
    _verify_webhook_token(authorization)
    # Use the migrated normalization logic to parse the incoming webhook
    try:
        from alerts.domain.normalization import normalize_alert_payload
        alerts_list = normalize_alert_payload(payload)
    except Exception as e:
        logger.error(f"Failed to normalize alert payload: {e}")
        from alerts.domain.alert import Alert
        # Fallback to keep ingestion moving
        alerts_list = [Alert.from_parts(
            name="Unknown Alert", 
            source="unknown", 
            severity="warning", 
            labels={"namespace": "default"}, 
            raw_payload=payload
        )]
    
    repo = db.SqliteInvestigationRepository()
    investigation_ids = []
    
    for alert in alerts_list:
        investigation_id = str(uuid.uuid4())
        
        from alerts.domain.investigation import Investigation
        from alerts.domain.enums import InvestigationStatus
        
        investigation = Investigation(
            investigation_id=investigation_id,
            alert=alert,
            status=InvestigationStatus.RECEIVED,
            created_at=datetime.now(UTC)
        )
        
        await repo.save(investigation)
        investigation_ids.append(investigation_id)
        
        # Phase 2: Start the orchestrator here
        # We need to run the orchestrator in the background task
        # But we need to define orchestrate_investigation function first.
        # Wait, I'll define it above and add it here.
        background_tasks.add_task(orchestrate_investigation, alert, investigation_id, repo)
    
    return AlertWebhookResponse(
        investigation_ids=investigation_ids,
        status="accepted"
    )

class ManualInvestigationRequest(BaseModel):
    target: str

class ManualInvestigationResponse(BaseModel):
    investigation_id: str

@router.post("/manual", response_model=ManualInvestigationResponse)
async def trigger_manual_investigation(
    req_body: ManualInvestigationRequest,
    request: Request,
    background_tasks: BackgroundTasks
) -> ManualInvestigationResponse:
    if auth.auth_enabled():
        if not getattr(request.state, "user", None):
            raise HTTPException(status_code=401, detail="Unauthorized")
        user_id = request.state.user.get("id", "unknown")
    else:
        user_id = "local"
    
    parts = req_body.target.split("/")
    if len(parts) == 1:
        ns, kind, name = "default", "pod", parts[0]
    elif len(parts) == 2:
        ns, kind, name = "default", parts[0], parts[1]
    else:
        ns, kind, name = parts[0], parts[1], "/".join(parts[2:])

    import sys
    mcp_path = _resolve_mcp_path()
    if mcp_path not in sys.path:
        sys.path.insert(0, mcp_path)

    # Fail before any kubectl runs. find_workload below is a cluster-wide
    # `kubectl get pods --all-namespaces`, so discovering "no cluster chosen"
    # afterwards would already have queried the wrong one.
    try:
        cluster_session.resolve_default()
    except (cluster_session.NoDefaultCluster,
            cluster_session.ClusterConnectionUnavailable) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # For pod targets we always run find_workload — both to auto-discover the
    # namespace (when the user typed a bare name or `pod/<name>`) AND to read
    # the pod's effective status, which we use below to alias the synthetic
    # alertname to a specialty playbook (CrashLoopBackOff -> crashloopbackoff,
    # OOMKilled -> oomkilled). Without the alias /rca on a crashlooping pod
    # would route to generic-pod and miss the POC's branches/bases/scoring.
    # Fail-soft: if discovery errors or finds nothing, keep the namespace
    # fallback and skip specialty aliasing — investigation still runs.
    discovered_status = ""
    if kind == "pod":
        try:
            from k8s.wrappers import find_workload
            # find_workload runs a blocking `kubectl get pods --all-namespaces`
            # subprocess — offload to a worker thread so the event loop stays
            # responsive while kubectl runs (1-3s on large clusters).
            # Bound to the chosen cluster: run_in_threadpool copies the
            # current context, so the runner contextvar carries into it.
            with _bound_to_chosen_cluster():
                result = await run_in_threadpool(find_workload, name)
            # find_workload returns flattened dicts with top-level "namespace",
            # "name", and "status" (the *effective* status — already collapses
            # CrashLoopBackOff/ImagePullBackOff/OOMKilled waiting/terminated
            # reasons into a single field; see k8s/wrappers.py:137-148). It
            # does substring matching, so for "jenkins-legacy-0" it might
            # return "jenkins-legacy-0" AND "jenkins-legacy-01" — filter to
            # exact name matches so we don't auto-pick a similar pod.
            matches = result.get("pods", [])
            exact = [
                m for m in matches
                if m.get("name") == name and m.get("namespace")
            ]

            if len(parts) <= 2:
                # namespace was defaulted — auto-discover
                discovered_ns = sorted({m["namespace"] for m in exact})
                if len(discovered_ns) == 1:
                    ns = discovered_ns[0]
                    logger.info(
                        f"/manual: auto-discovered namespace '{ns}' for pod '{name}'"
                    )
                elif len(discovered_ns) > 1:
                    ns = discovered_ns[0]
                    logger.warning(
                        f"/manual: pod '{name}' exists in multiple namespaces "
                        f"{discovered_ns}; picked '{ns}' (alphabetical first). "
                        "Specify <ns>/pod/<name> to disambiguate."
                    )
                elif matches:
                    # find_workload returned substring matches but no exact-name
                    # hit — surface the similar names so it's visible in logs
                    # why we're falling back to "default".
                    similar = [
                        f"{m.get('namespace') or '?'}/{m.get('name') or '?'}"
                        for m in matches[:5]
                    ]
                    logger.warning(
                        f"/manual: no exact-name match for pod '{name}'; "
                        f"find_workload returned {len(matches)} similar name(s): "
                        f"{similar}"
                    )

            # Read the effective status of the pod we'll investigate. Prefer
            # an exact (ns, name) match; otherwise the first exact-name match.
            for m in exact:
                if m.get("namespace") == ns:
                    discovered_status = m.get("status", "") or ""
                    break
            if not discovered_status and exact:
                discovered_status = exact[0].get("status", "") or ""
        except Exception as exc:
            logger.warning(f"/manual: find_workload discovery failed for '{name}': {exc}")

    from alerts.domain.alert import Alert
    from alerts.domain.enums import AlertSource
    from alerts.domain.investigation import Investigation
    from alerts.domain.enums import InvestigationStatus

    # Smart routing for /rca: alias the synthetic alertname to one of the
    # specialty alertnames the classifier already routes to a POC playbook,
    # so /rca on a CrashLoopBackOff pod gets the same crashloop investigation
    # depth that a real Alertmanager-fired KubernetesPodCrashLooping alert
    # would get. If we don't have a specialty match for the observed status
    # (or it's a workload, not a pod), keep the Manual* alertname so the
    # exact-override sends it to generic-pod/generic-workload.
    extra_labels: dict[str, str] = {}
    if kind == "pod":
        if "CrashLoopBackOff" in discovered_status:
            specialty_alias = ("KubernetesPodCrashLooping", {"reason": "CrashLoopBackOff"})
        elif "OOMKilled" in discovered_status:
            specialty_alias = ("ContainerOOMKilled", {"reason": "OOMKilled"})
        else:
            specialty_alias = None
        if specialty_alias:
            alert_name, extra_labels = specialty_alias
            logger.info(
                f"/manual: pod {ns}/{name} status={discovered_status!r} -> routing "
                f"as alertname={alert_name!r} for specialty playbook"
            )
        else:
            alert_name = "ManualPodInvestigation"
    else:
        alert_name = "ManualWorkloadInvestigation"

    # Set the canonical workload-shape label too (pod=<name> for pods,
    # deployment=<name> for deployments, etc.). The POC specialty playbooks
    # template {{ alert.labels.pod }} — without this label the renderer would
    # skip every step with that template (see engine._render_args) and the
    # investigation would have no evidence.
    workload_label = {
        "pod": "pod",
        "deployment": "deployment",
        "statefulset": "statefulset",
        "daemonset": "daemonset",
    }.get(kind, "resource")

    alert = Alert.from_parts(
        name=alert_name,
        source=AlertSource.MANUAL,
        severity="info",
        labels={
            "namespace": ns,
            "kind": kind,
            "resource": name,
            workload_label: name,
            **extra_labels,
        },
        raw_payload={"target": req_body.target, "discovered_status": discovered_status},
    )
    
    repo = db.SqliteInvestigationRepository()
    investigation_id = str(uuid.uuid4())
    
    investigation = Investigation(
        investigation_id=investigation_id,
        alert=alert,
        status=InvestigationStatus.RECEIVED,
        created_at=datetime.now(UTC)
    )
    investigation.append_audit("manual_trigger", {"user_id": user_id, "target": req_body.target})
    
    await repo.save(investigation)
    background_tasks.add_task(orchestrate_investigation, alert, investigation_id, repo)
    
    return ManualInvestigationResponse(investigation_id=investigation_id)

@router.get("")
async def list_alerts(limit: int = 50):
    with db._conn() as con:
        rows = con.execute(
            """
            SELECT id, namespace, severity, source, status, created_at, document 
            FROM investigations 
            ORDER BY created_at DESC 
            LIMIT ?
            """, 
            (limit,)
        ).fetchall()
    
    import json
    results = []
    for row in rows:
        results.append({
            "id": row["id"],
            "namespace": row["namespace"],
            "severity": row["severity"],
            "source": row["source"],
            "status": row["status"],
            "created_at": row["created_at"],
            "document": json.loads(row["document"])
        })
    return {"alerts": results}
