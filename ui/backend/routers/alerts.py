import hmac
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

import alert_correlation
import alert_silences
import auth
import db
import log_safety

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


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


def _webhook_settings():
    """Settings, read per call rather than at import.

    The router is imported while `main` is still assembling the app, before
    tests (and `desktop_main`) have finished setting the environment. Reading
    at import time would freeze whatever was set at that moment.
    """
    from config.settings import get_settings

    return get_settings()


def reset_webhook_settings() -> None:
    """Drop the memoised Settings so a changed environment takes effect.

    `get_settings` is `lru_cache`d. Exported for tests and for anything that
    rewrites the environment after start-up.
    """
    from config.settings import get_settings

    get_settings.cache_clear()


def _require_webhook_enabled() -> None:
    """Refuse the webhook unless the operator asked for it.

    404 rather than 403: a deployment that has not enabled alert ingestion
    should not advertise that it could. The detail names the variable anyway,
    because the person reading this response is the operator turning it on —
    the path is in the public README, so naming the flag tells an attacker
    nothing they could not already read.
    """
    if _webhook_settings().alertmanager_webhook_enabled:
        return
    raise HTTPException(
        status_code=404,
        detail=(
            "Alert ingestion is disabled. Set ALERTMANAGER_WEBHOOK_ENABLED=true "
            "to accept Alertmanager webhooks, and set ALERT_WEBHOOK_TOKEN so "
            "the endpoint is not open."
        ),
    )


def _verify_webhook_token(authorization: str | None) -> None:
    """Authenticate machine-to-machine webhook callers via a shared bearer token.

    The webhook is exempt from interactive user-session auth (it is listed in
    auth.is_public_path so Alertmanager isn't 401'd by the session middleware),
    so this is the security boundary for it. When ALERT_WEBHOOK_TOKEN is set we
    require a matching `Authorization: Bearer <token>`; when it is unset the
    webhook stays open for local/dev use."""
    expected = os.environ.get("ALERT_WEBHOOK_TOKEN")
    if not expected:
        # Permitted — a local Alertmanager pointed at a laptop is a real setup,
        # and requiring a token there would break it. But nothing else in the
        # system will mention that cluster investigations are now reachable
        # unauthenticated, so say it on every call rather than once at startup:
        # the operator who needs to see this is reading logs because traffic
        # arrived, not because the process booted.
        logger.warning(
            "Alert webhook accepted an unauthenticated request: "
            "ALERTMANAGER_WEBHOOK_ENABLED is set but ALERT_WEBHOOK_TOKEN is not. "
            "Anyone who can reach this backend can start investigations."
        )
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

        await orchestrator.investigate(alert, investigation_id)
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
    # How many of the ids above are existing investigations rather than new
    # ones. Lets an operator see dedup working without reading the logs.
    deduplicated: int = 0
    # How many were `resolved` deliveries that closed an open investigation
    # instead of starting one.
    resolved: int = 0
    # How many matched an active silence and were recorded without starting
    # anything.
    silenced: int = 0
    # How many new investigations were attached to an incident, existing or
    # newly opened.
    correlated: int = 0

@router.post("/webhook", response_model=AlertWebhookResponse)
async def receive_webhook(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
) -> AlertWebhookResponse:
    _require_webhook_enabled()
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
    
    deduped = 0

    resolved = 0
    silenced = 0
    correlated = 0

    settings = _webhook_settings()

    # Read once per request, not per alert: Alertmanager posts batches, and the
    # silence set cannot meaningfully change inside one.
    try:
        active_silences = db.list_active_silences()
    except Exception as e:  # pragma: no cover — defensive
        # A silence lookup that fails must not stop alert ingestion. Failing
        # open means investigating something that should have been quiet, which
        # is noisy; failing closed would mean dropping alerts entirely.
        logger.error("silence lookup failed, ingesting unsilenced: %s", e)
        active_silences = []

    for alert in alerts_list:
        fingerprint = getattr(alert, "fingerprint", "")

        # A `resolved` delivery says the problem went away. Before this it fell
        # through and started a *new* investigation into a condition that had
        # already stopped — an LLM run, a row and a notification, all for an
        # alert that was over. `status` is deliberately not part of the
        # fingerprint, so the resolved delivery matches the firing one.
        if str(getattr(alert, "status", "firing")).lower() == "resolved":
            existing = db.find_open_investigation(fingerprint)
            if existing:
                seconds = db.resolve_investigation(existing["id"])
                logger.info(
                    "alert %s resolved after %.0fs; closing investigation %s",
                    log_safety.one_line(alert.name),
                    seconds or 0.0,
                    existing["id"],
                )
                investigation_ids.append(existing["id"])
                resolved += 1
                # The alert clearing may have settled the last open
                # investigation on its incident.
                try:
                    if existing.get("incident_id"):
                        db.close_incident_if_settled(existing["incident_id"])
                except Exception as e:  # pragma: no cover — defensive
                    logger.error("incident close failed: %s", e)
            else:
                # Nothing open to close: the firing delivery predates this
                # feature, was already closed, or never arrived. Still not a
                # reason to investigate something that has stopped.
                logger.info(
                    "alert %s resolved with no open investigation; ignoring",
                    log_safety.one_line(alert.name),
                )
            continue

        # Silences are checked after resolved handling and before dedup. A
        # resolved delivery must still be able to close an investigation opened
        # before the silence existed — otherwise silencing an alert mid-incident
        # would strand that investigation open forever, and it would go on
        # absorbing every later occurrence.
        matching = alert_silences.find_matching(active_silences, dict(alert.labels))
        if matching:
            for silence in matching:
                db.record_silence_match(silence["id"])
            logger.info(
                "alert %s silenced by %s (%s)",
                log_safety.one_line(alert.name),
                log_safety.one_line(matching[0]["id"]),
                log_safety.one_line(matching[0]["reason"]),
            )
            silenced += 1
            continue

        # Alertmanager re-sends a firing alert every repeat_interval for as
        # long as the condition holds. Without this, one ongoing problem
        # started a fresh investigation — and a fresh LLM run — on every
        # delivery. The fingerprint was already computed on every alert and
        # read by nothing.
        existing = db.find_open_investigation(fingerprint)
        if existing:
            count = db.record_recurrence(existing["id"])
            logger.info(
                "alert %s recurred (x%s); reusing investigation %s",
                log_safety.one_line(alert.name),
                count,
                existing["id"],
            )
            # The caller gets the live investigation's id, not silence: an
            # Alertmanager receiver that sees no id has no way to tell dedup
            # from ingestion having failed.
            investigation_ids.append(existing["id"])
            deduped += 1
            continue

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

        # Correlation is an enhancement to ingestion, never a gate on it: an
        # alert that cannot be grouped still gets investigated on its own.
        try:
            namespace, workload = alert_correlation.correlation_key(dict(alert.labels))
            incident_id = db.find_or_open_incident(
                namespace,
                workload,
                window_minutes=settings.alert_correlation_window_minutes,
                max_lifetime_hours=settings.alert_incident_max_lifetime_hours,
            )
            if incident_id:
                # Set after save(): the orchestrator rewrites this row from an
                # object built before correlation ran, so writing it earlier
                # would be clobbered back to NULL.
                db.attach_to_incident(investigation_id, incident_id)
                correlated += 1
        except Exception as e:  # pragma: no cover — defensive
            logger.error("correlation failed for %s: %s", investigation_id, e)
        
        # Phase 2: Start the orchestrator here
        # We need to run the orchestrator in the background task
        # But we need to define orchestrate_investigation function first.
        # Wait, I'll define it above and add it here.
        background_tasks.add_task(orchestrate_investigation, alert, investigation_id, repo)
    
    return AlertWebhookResponse(
        investigation_ids=investigation_ids,
        status="accepted",
        deduplicated=deduped,
        resolved=resolved,
        silenced=silenced,
        correlated=correlated,
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

@router.get("/incidents")
async def list_incidents(limit: int = 50, include_closed: bool = False) -> dict:
    """Alerts grouped by the problem they are about.

    Declared before the `/{...}` reading routes below so `incidents` is not
    swallowed as a path parameter.
    """
    incidents = await run_in_threadpool(
        db.list_incidents, limit=limit, include_closed=include_closed
    )
    return {"incidents": incidents, "count": len(incidents)}


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: str) -> dict:
    incident = await run_in_threadpool(db.get_incident, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


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
