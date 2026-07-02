import os
import sys
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

# Ensure MCP_PATH is set so main.py can load
MCP_PATH = str(Path(__file__).resolve().parent.parent.parent.parent / "mcp")
os.environ["MCP_PATH"] = MCP_PATH
if MCP_PATH not in sys.path:
    sys.path.insert(0, MCP_PATH)

from main import app
import db
import auth
import routers.alerts as alerts_router

ALERT_PAYLOAD = {
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "HighCPU", "severity": "critical", "namespace": "demo"},
            "annotations": {"description": "CPU is high"},
        }
    ],
}


@pytest.fixture(autouse=True)
def setup_db():
    """Ensure tests run against a clean, initialized DB."""
    # Using an in-memory DB or temporary file would be better, 
    # but for this test we'll rely on the app's default behavior 
    # and clean up the investigations table.
    db.init_db()
    with db._conn() as con:
        con.execute("DELETE FROM investigations")
    yield
    with db._conn() as con:
        con.execute("DELETE FROM investigations")

@pytest.fixture(autouse=True)
def mock_qdrant(monkeypatch):
    """Force an in-memory Qdrant client for all tests (mocking private state)."""
    from qdrant_client import QdrantClient
    from services.vector_db import vector_db
    vector_db._client = QdrantClient(":memory:")


def test_alerts_webhook_and_list():
    client = TestClient(app)
    
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "HighCPU", "severity": "critical", "namespace": "demo"},
                "annotations": {"description": "CPU is high"}
            }
        ]
    }

    # 1. Post to webhook
    post_resp = client.post("/api/v1/alerts/webhook", json=payload)
    assert post_resp.status_code == 200
    data = post_resp.json()
    assert data["status"] == "accepted"
    assert len(data["investigation_ids"]) == 1

    # 2. Get alerts
    get_resp = client.get("/api/v1/alerts")
    assert get_resp.status_code == 200
    alerts = get_resp.json().get("alerts", [])
    assert len(alerts) == 1
    
    alert = alerts[0]
    assert alert["namespace"] == "demo"
    assert alert["severity"] == "critical"
    assert alert["source"] == "alertmanager"
    assert alert["status"] in ["received", "running", "completed", "failed"]


# ── Deployment regression tests ───────────────────────────────────────────────
# These guard the two bugs that made the webhook non-functional in the Helm
# deployment even though it worked locally: (1) the session-auth middleware
# 401'd the webhook, and (2) the playbook path was resolved by a relative
# traversal that breaks in the flattened container image. See commit 25ff5bf.


def test_webhook_exempt_from_session_auth():
    """The webhook must be a public path so AUTH_ENABLED=true does not 401
    machine callers (Alertmanager has no user session)."""
    assert auth.is_public_path("/api/v1/alerts/webhook", "POST") is True
    # The operational list endpoint must NOT be public — it stays behind auth.
    assert auth.is_public_path("/api/v1/alerts", "GET") is False


def test_webhook_token_enforced(monkeypatch):
    """When ALERT_WEBHOOK_TOKEN is set, the webhook requires a matching bearer
    token; missing or wrong tokens are rejected, the correct one is accepted."""
    monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", "supersecret-token-123")
    client = TestClient(app)

    assert client.post("/api/v1/alerts/webhook", json=ALERT_PAYLOAD).status_code == 401
    assert client.post(
        "/api/v1/alerts/webhook", json=ALERT_PAYLOAD,
        headers={"Authorization": "Bearer wrong"},
    ).status_code == 401
    ok = client.post(
        "/api/v1/alerts/webhook", json=ALERT_PAYLOAD,
        headers={"Authorization": "Bearer supersecret-token-123"},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "accepted"


def test_webhook_open_when_token_unset(monkeypatch):
    """With no ALERT_WEBHOOK_TOKEN configured the webhook stays open (dev mode)."""
    monkeypatch.delenv("ALERT_WEBHOOK_TOKEN", raising=False)
    client = TestClient(app)
    assert client.post("/api/v1/alerts/webhook", json=ALERT_PAYLOAD).status_code == 200


def test_webhook_rejects_malformed_auth(monkeypatch):
    """Empty bearer and a bare token (no scheme) are rejected; a lowercase
    `bearer` scheme is accepted (the scheme is case-insensitive per RFC 6750)."""
    monkeypatch.setenv("ALERT_WEBHOOK_TOKEN", "tok123")
    client = TestClient(app)
    post = lambda h: client.post("/api/v1/alerts/webhook", json=ALERT_PAYLOAD, headers=h)
    assert post({"Authorization": "Bearer "}).status_code == 401   # empty token
    assert post({"Authorization": "tok123"}).status_code == 401    # missing scheme
    assert post({"Authorization": "bearer tok123"}).status_code == 200  # case-insensitive


def test_webhook_empty_batch_is_accepted(monkeypatch):
    """An empty alerts array is handled gracefully: 200 with no investigations."""
    monkeypatch.delenv("ALERT_WEBHOOK_TOKEN", raising=False)
    client = TestClient(app)
    resp = client.post("/api/v1/alerts/webhook", json={"alerts": []})
    assert resp.status_code == 200
    assert resp.json()["investigation_ids"] == []


def test_playbook_path_anchored_to_mcp_path():
    """Playbooks must resolve under MCP_PATH and exist, not via a relative
    traversal that overshoots in the container image."""
    mcp_path = os.path.realpath(os.environ["MCP_PATH"])
    resolved = os.path.realpath(alerts_router._resolve_playbook_path())
    assert resolved == os.path.join(mcp_path, "data", "playbooks")
    assert os.path.isdir(resolved), f"playbook dir missing: {resolved}"

# ── Manual Trigger Tests ───────────────────────────────────────────────────────

@pytest.mark.parametrize("auth_enabled, expected_status", [
    (False, 200),
    (True, 401)
])
def test_manual_trigger_auth(monkeypatch, auth_enabled, expected_status):
    if auth_enabled:
        monkeypatch.setenv("AUTH_ENABLED", "true")
    else:
        monkeypatch.setenv("AUTH_ENABLED", "false")
    
    # Reload auth to pick up new env var if needed, or we rely on auth.auth_enabled() reading dynamically.
    # Actually auth.auth_enabled() is usually dynamic: `return os.environ.get("AUTH_ENABLED", "false").lower() == "true"`
    
    client = TestClient(app)
    resp = client.post("/api/v1/alerts/manual", json={"target": "pod/frontend"})
    assert resp.status_code == expected_status

def test_manual_trigger_e2e(monkeypatch):
    import time
    import json
    
    monkeypatch.setenv("AUTH_ENABLED", "false")
    client = TestClient(app)
    
    # Trigger manual investigation for a deployment
    resp = client.post("/api/v1/alerts/manual", json={"target": "prod/statefulset/api"})
    assert resp.status_code == 200
    data = resp.json()
    inv_id = data["investigation_id"]
    assert inv_id
    
    # Wait for the background orchestrator to complete
    max_wait = 10
    for _ in range(max_wait):
        with db._conn() as con:
            row = con.execute("SELECT * FROM investigations WHERE id = ?", (inv_id,)).fetchone()
            if row and row["status"] in ("completed", "failed"):
                break
        time.sleep(1)
        
    assert row is not None
    assert row["status"] == "completed"
    
    doc = json.loads(row["document"])
    assert doc.get("selected_playbook") == "generic-workload"
    
    # Verify the executed tool had the mapped workload_type
    executed_tools = doc.get("executed_tools", [])
    workload_tool_found = any(
        t.get("tool") == "investigate_workload" and
        t.get("args", {}).get("workload_type") == "statefulset"
        for t in executed_tools
    )
    assert workload_tool_found, "investigate_workload was not called with workload_type='statefulset'"

    # The manual_trigger audit event stamped by the /manual router must survive
    # the orchestrator run — i.e. the orchestrator must continue the existing
    # Investigation record, not overwrite it. This guards regression of the bug
    # where the orchestrator created a fresh Investigation and clobbered the
    # router's audit stamp, losing the user attribution.
    audit_log = doc.get("audit_log", [])
    manual_events = [e for e in audit_log if e.get("event_type") == "manual_trigger"]
    assert len(manual_events) == 1, (
        f"expected exactly one manual_trigger audit event, got {len(manual_events)}; "
        f"audit_log event types: {[e.get('event_type') for e in audit_log]}"
    )
    assert manual_events[0]["payload"].get("user_id") == "local", (
        f"manual_trigger event missing user_id=local in dev mode; payload: {manual_events[0]['payload']}"
    )

    # Verify that the investigation was successfully saved to semantic incident memory in Qdrant
    import asyncio
    from services.vector_db import vector_db
    from alerts.repositories.qdrant import QdrantSemanticMemoryRepository
    
    sem_repo = QdrantSemanticMemoryRepository(vector_db, "incident_memory")
    records = asyncio.run(sem_repo.search("statefulset"))
    assert len(records) > 0, "RCA was not stored in Qdrant semantic memory"
    assert records[0].investigation_id == inv_id
    assert records[0].alert_name == "ManualWorkloadInvestigation"
    assert records[0].workload == "api"


def test_metrics_endpoint_is_public_and_exposes_counters(monkeypatch):
    """The /api/v1/metrics endpoint must be scrapeable without a user session
    (so Prometheus can hit it) and must expose at least the investigation
    lifecycle counters after a webhook fires."""
    monkeypatch.setenv("AUTH_ENABLED", "true")  # public-path check still works under auth
    client = TestClient(app)
    # Fire an investigation so counters tick up.
    client.post("/api/v1/alerts/webhook", json=ALERT_PAYLOAD)
    resp = client.get("/api/v1/metrics")
    assert resp.status_code == 200
    body = resp.text
    assert "investigations_started_total" in body, (
        f"counter not in exposition; body: {body[:300]}"
    )
    # Exposition format: each metric is a "name value" line.
    assert any(
        line.startswith("investigations_started_total ") for line in body.splitlines()
    ), f"investigations_started_total line missing; body: {body[:300]}"


def test_manual_trigger_auto_discovers_namespace(monkeypatch):
    """/rca <bare-name> should call find_workload to discover the namespace
    instead of always defaulting to "default". Mocks find_workload to return
    the flattened shape it actually produces (top-level namespace/name keys,
    NOT raw Pod metadata) — this is the exact shape the namespace-traversal
    bug in 8e5f365/3ec57c1 missed: those traversed m["metadata"]["namespace"]
    which is always None for find_workload's return type.
    """
    monkeypatch.setenv("AUTH_ENABLED", "false")

    # Patch find_workload at the module path the alerts router imports it from.
    # NOTE: the router does `from k8s.wrappers import find_workload` inside the
    # function body, so we patch at k8s.wrappers (where it's defined).
    import k8s.wrappers as wrappers

    def fake_find_workload(name, environment=None):
        # Return the exact shape find_workload produces — flattened, top-level.
        if name == "jenkins-legacy-0":
            return {
                "query": name,
                "pods": [
                    {"namespace": "ci", "name": "jenkins-legacy-0", "phase": "Running"},
                    # Plus a substring-match noise pod (different name) that must
                    # NOT cause us to pick its namespace:
                    {"namespace": "other", "name": "jenkins-legacy-01", "phase": "Running"},
                ],
            }
        return {"query": name, "pods": []}

    monkeypatch.setattr(wrappers, "find_workload", fake_find_workload)

    client = TestClient(app)
    resp = client.post("/api/v1/alerts/manual", json={"target": "jenkins-legacy-0"})
    assert resp.status_code == 200
    inv_id = resp.json()["investigation_id"]

    # Read the row immediately — we only need to inspect the persisted alert
    # labels, not wait for the orchestrator to complete.
    with db._conn() as con:
        row = con.execute(
            "SELECT namespace, document FROM investigations WHERE id = ?",
            (inv_id,),
        ).fetchone()
    assert row is not None
    import json
    doc = json.loads(row["document"])
    labels = doc.get("alert", {}).get("labels", {})
    assert labels.get("namespace") == "ci", (
        f"namespace auto-discovery failed; expected 'ci', got '{labels.get('namespace')}'. "
        f"This is the regression that shipped silently in 8e5f365 — the router "
        f"traversed m['metadata']['namespace'] but find_workload returns top-level "
        f"'namespace'/'name' keys."
    )
    assert row["namespace"] == "ci"


@pytest.mark.parametrize(
    "pod_status,expected_alertname,expected_reason",
    [
        ("CrashLoopBackOff", "KubernetesPodCrashLooping", "CrashLoopBackOff"),
        ("Init:CrashLoopBackOff", "KubernetesPodCrashLooping", "CrashLoopBackOff"),
        ("OOMKilled",        "ContainerOOMKilled",       "OOMKilled"),
    ],
)
def test_manual_trigger_smart_routes_pod_state_to_specialty_alertname(
    monkeypatch, pod_status, expected_alertname, expected_reason
):
    """/rca on a pod in a state we have a POC specialty playbook for must
    alias the synthetic alertname so the classifier routes through that
    specialty rule — not the generic-pod ManualPodInvestigation override.
    Without this aliasing /rca on a crashlooping pod silently gets the
    shallow generic walkthrough even though we have the full crashloopbackoff
    playbook (branches + scoring) loaded. This was the original user-reported
    gap that motivated the POC playbook restore.
    """
    monkeypatch.setenv("AUTH_ENABLED", "false")
    import k8s.wrappers as wrappers

    def fake_find_workload(name, environment=None):
        return {
            "query": name,
            "pods": [
                {"namespace": "prod", "name": name, "phase": "Running", "status": pod_status},
            ],
        }

    monkeypatch.setattr(wrappers, "find_workload", fake_find_workload)

    client = TestClient(app)
    resp = client.post("/api/v1/alerts/manual", json={"target": "api-7f"})
    assert resp.status_code == 200
    inv_id = resp.json()["investigation_id"]

    with db._conn() as con:
        row = con.execute(
            "SELECT document FROM investigations WHERE id = ?", (inv_id,),
        ).fetchone()
    assert row is not None
    import json as _json
    doc = _json.loads(row["document"])
    alert = doc.get("alert", {})
    labels = alert.get("labels", {})

    assert alert.get("name") == expected_alertname, (
        f"smart routing should alias alertname to {expected_alertname!r} for "
        f"pod_status={pod_status!r}, got {alert.get('name')!r}"
    )
    assert labels.get("reason") == expected_reason, (
        f"smart routing should set reason={expected_reason!r}, got {labels.get('reason')!r}"
    )
    assert labels.get("pod") == "api-7f", (
        f"smart routing should set pod label so specialty playbooks render, "
        f"got labels={labels!r}"
    )
    assert labels.get("namespace") == "prod"


def test_crashloop_playbook_runs_investigate_pod_deterministically_first(monkeypatch):
    """/rca on a CrashLoopBackOff pod must dispatch investigate_pod *before*
    the LLM gets to pick. Background: prior to this guarantee the LLM
    consistently picked raw `get_pod_logs` (defaulting to the first
    container in the spec, which never started on Jenkins-like pods with
    a failing init container) and missed the AggregatePluginPrerequisites
    error entirely. investigate_pod auto-selects the failing container —
    same composite tool the /chat path uses — and is the single biggest
    determinant of RCA depth. The crashloopbackoff playbook marks it
    `deterministic: true`; this test guards that marker AND the orchestrator's
    pre-LLM execution path.
    """
    import time
    monkeypatch.setenv("AUTH_ENABLED", "false")
    import k8s.wrappers as wrappers

    monkeypatch.setattr(wrappers, "find_workload", lambda name, environment=None: {
        "query": name,
        "pods": [{
            "namespace": "jenkins-legacy",
            "name": name,
            "phase": "Running",
            "status": "CrashLoopBackOff",
        }],
    })

    client = TestClient(app)
    resp = client.post("/api/v1/alerts/manual", json={"target": "jenkins-legacy-0"})
    assert resp.status_code == 200
    inv_id = resp.json()["investigation_id"]

    import json as _json
    row = None
    for _ in range(30):
        with db._conn() as con:
            row = con.execute(
                "SELECT document, status FROM investigations WHERE id = ?",
                (inv_id,),
            ).fetchone()
        if row and row["status"] in ("completed", "failed"):
            break
        time.sleep(1)
    assert row is not None
    doc = _json.loads(row["document"])

    assert doc.get("selected_playbook") == "crashloopbackoff", (
        f"alert should route to crashloopbackoff, got {doc.get('selected_playbook')!r}"
    )

    executed = doc.get("executed_tools", [])
    assert executed, "no tools were executed"
    assert executed[0]["tool"] == "investigate_pod", (
        f"investigate_pod must run deterministically before the LLM steers, "
        f"but the first tool dispatched was {executed[0]['tool']!r}. "
        f"Full sequence: {[t['tool'] for t in executed]}"
    )

    audit = doc.get("audit_log", [])
    det_actions = [
        e for e in audit
        if e.get("event_type") == "action_validated"
        and (e.get("payload") or {}).get("reason") == "deterministic playbook step"
    ]
    assert det_actions, (
        f"expected at least one deterministic action_validated event; "
        f"audit event types: {[e.get('event_type') for e in audit]}"
    )


def test_manual_trigger_healthy_pod_still_uses_generic_pod(monkeypatch):
    """/rca on a pod with no specialty-eligible status must keep the
    ManualPodInvestigation alertname so the manual-pod exact-override sends
    it to generic-pod. Smart aliasing must not over-fire."""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    import k8s.wrappers as wrappers

    monkeypatch.setattr(wrappers, "find_workload", lambda name, environment=None: {
        "query": name,
        "pods": [{"namespace": "prod", "name": name, "phase": "Running", "status": "Running"}],
    })

    client = TestClient(app)
    resp = client.post("/api/v1/alerts/manual", json={"target": "api-7f"})
    inv_id = resp.json()["investigation_id"]
    with db._conn() as con:
        row = con.execute("SELECT document FROM investigations WHERE id = ?", (inv_id,)).fetchone()
    import json as _json
    alert = _json.loads(row["document"])["alert"]
    assert alert["name"] == "ManualPodInvestigation"
    assert alert["labels"].get("reason") is None
    assert alert["labels"].get("pod") == "api-7f"


def test_manual_trigger_workload_still_uses_generic_workload(monkeypatch):
    """/rca <ns>/deployment/<name> must keep ManualWorkloadInvestigation and
    set deployment=<name> as the workload-shape label. Smart pod-routing
    must not touch the workload path."""
    monkeypatch.setenv("AUTH_ENABLED", "false")

    client = TestClient(app)
    resp = client.post("/api/v1/alerts/manual", json={"target": "prod/deployment/api"})
    inv_id = resp.json()["investigation_id"]
    with db._conn() as con:
        row = con.execute("SELECT document FROM investigations WHERE id = ?", (inv_id,)).fetchone()
    import json as _json
    alert = _json.loads(row["document"])["alert"]
    assert alert["name"] == "ManualWorkloadInvestigation"
    assert alert["labels"]["namespace"] == "prod"
    assert alert["labels"]["kind"] == "deployment"
    assert alert["labels"]["deployment"] == "api"
    assert alert["labels"].get("pod") is None


def test_classifier_routes_specialty_alerts_to_poc_playbooks():
    """The full POC playbook content (crashloopbackoff, oomkilled, highcpuusage,
    dependency_call_timeout, workload_availability, loki_logs_health, generic)
    plus branches/bases must be migrated and reachable via classification rules.

    This guards against regression of the POC restore: if any specialty
    playbook is removed from data/playbooks/alert_playbooks/ or any routing
    rule is stripped from classification-rules.yaml, the corresponding alert
    will silently fall through to `generic` and the user loses the depth
    they had in the POC.
    """
    from alerts.playbooks.classifier import AlertClassifier
    from alerts.playbooks.loader import PlaybookLoader
    from alerts.playbooks.registry import PlaybookRegistry
    from alerts.domain.alert import Alert

    path = alerts_router._resolve_playbook_path()
    reg = PlaybookRegistry(PlaybookLoader(path))
    playbooks = reg.list()
    by_id = {p.id for p in playbooks}
    # All migrated specialty playbooks must be loaded.
    for required in [
        "crashloopbackoff",
        "oomkilled",
        "highcpuusage",
        "dependency_call_timeout",
        "kubernetes_workload_availability",
        "loki_logs_health",
        "generic",
        "generic-pod",
        "generic-workload",
    ]:
        assert required in by_id, f"playbook missing: {required}. Loaded: {sorted(by_id)}"

    cls = AlertClassifier(playbooks, rules_path=os.path.join(path, "classification-rules.yaml"))

    routings = [
        ("KubernetesPodCrashLooping", {"reason": "CrashLoopBackOff"}, "crashloopbackoff"),
        ("ContainerOOMKilled", {}, "oomkilled"),
        ("HighCPUUsage", {}, "highcpuusage"),
        ("DependencyCallTimeout", {}, "dependency_call_timeout"),
        ("KubernetesDeploymentReplicasMismatch", {}, "kubernetes_workload_availability"),
        ("LokiRequestErrors", {}, "loki_logs_health"),
        # Manual /rca overrides must STILL win even after specialty rules were added.
        ("ManualPodInvestigation", {"kind": "pod"}, "generic-pod"),
        ("ManualWorkloadInvestigation", {"kind": "deployment"}, "generic-workload"),
    ]
    for alertname, extra_labels, expected in routings:
        labels = {"alertname": alertname, **extra_labels}
        alert = Alert.from_parts(
            name=alertname, source="alertmanager", severity="critical",
            labels=labels, raw_payload={},
        )
        result = cls.classify(alert)
        assert result.playbook_id == expected, (
            f"alertname={alertname!r} should route to {expected!r}, "
            f"got {result.playbook_id!r}"
        )


def test_pod_rca_playbooks_use_existing_mcp_k8s_tools():
    """Pod RCA playbooks should be backed by the existing MCP tool registry.

    This catches the merge failure mode where POC-only executor names or a
    shallow manual playbook leave RCA with only describe/prometheus output
    instead of pod inventory, logs, events, and namespace/resource context.
    """
    from alerts.playbooks.loader import PlaybookLoader
    from alerts.playbooks.registry import PlaybookRegistry
    from tool_registry import resolve_tool

    path = alerts_router._resolve_playbook_path()
    playbooks = {p.id: p for p in PlaybookRegistry(PlaybookLoader(path)).list()}

    for playbook_id in ("generic-pod", "crashloopbackoff"):
        playbook = playbooks[playbook_id]
        for tool in playbook.allowed_tools:
            assert resolve_tool(tool) is not None, (
                f"{playbook_id} allows {tool!r}, but it is not in the MCP registry"
            )

    generic_pod_tools = {step.tool for step in playbooks["generic-pod"].steps}
    assert {
        "get_pods",
        "get_events",
        "get_pod_logs",
        "investigate_pod",
        "list_namespace_resources",
    }.issubset(generic_pod_tools)

    crashloop_tools = {step.tool for step in playbooks["crashloopbackoff"].steps}
    assert {
        "get_pods",
        "investigate_pod",
        "get_pod_logs",
        "list_namespace_resources",
        "prom_query",
    }.issubset(crashloop_tools)


def test_classifier_does_not_misroute_benign_restart_mentions():
    """The previous `\\brestart(s|ing|ed)?\\b` pattern in container_restart_crash
    matched any alert mentioning restart — including 'Node restarted
    successfully' and 'Deployment is restarting pods as part of rollout' —
    and silently triaged them as crashloops. The pattern is now scoped to
    pod/container context or an explicit crash/loop qualifier. Guard both
    the false-positive side and the still-routes side."""
    from alerts.playbooks.classifier import AlertClassifier
    from alerts.playbooks.loader import PlaybookLoader
    from alerts.playbooks.registry import PlaybookRegistry
    from alerts.domain.alert import Alert

    path = alerts_router._resolve_playbook_path()
    reg = PlaybookRegistry(PlaybookLoader(path))
    cls = AlertClassifier(reg.list(), rules_path=os.path.join(path, "classification-rules.yaml"))

    def classify(name, anno=None, labels=None):
        a = Alert.from_parts(
            name=name, source="alertmanager", severity="info",
            labels={"alertname": name, **(labels or {})},
            annotations=anno or {}, raw_payload={},
        )
        return cls.classify(a).playbook_id

    benign = [
        ("NodeRebooted", {"summary": "Node has been restarted successfully"}),
        ("DeploymentRollout", {"description": "Deployment is restarting pods as part of normal rollout"}),
        ("ServiceHealthy", {"summary": "Service restart completed in 12s"}),
    ]
    for name, anno in benign:
        assert classify(name, anno) != "crashloopbackoff", (
            f"benign alert {name!r} ({anno}) was misrouted to crashloopbackoff"
        )

    crashy = [
        ("KubernetesPodCrashLooping", {}, {}),
        ("ContainerRestart", {"summary": "container restart count exceeded threshold"}, {}),
        ("PodRestartStorm", {"summary": "pod-restart spike detected"}, {}),
        ("TooManyRestarts", {"summary": "Too many restarts in 5m"}, {}),
        ("FrequentRestarts", {"description": "frequent restarts observed"}, {}),
        ("RestartLoop", {"summary": "restart loop detected"}, {}),
        ("PodCrashing", {}, {"reason": "CrashLoopBackOff"}),
    ]
    for name, anno, labels in crashy:
        got = classify(name, anno, labels)
        assert got == "crashloopbackoff", (
            f"crashy alert {name!r} should route to crashloopbackoff, got {got}"
        )


def test_orchestrator_skips_steps_with_unresolved_template_labels():
    """When a playbook step templates {{ alert.labels.pod }} but the alert
    has no `pod` label, the renderer must flag the step as missing — so
    the orchestrator skips it rather than dispatching `pod=''` PromQL
    that returns empty results and misleading 'no evidence' conclusions.
    """
    from alerts.orchestrator.engine import InvestigationOrchestrator
    from alerts.domain.alert import Alert

    orch = InvestigationOrchestrator.__new__(InvestigationOrchestrator)

    alert_with_pod = Alert.from_parts(
        name="X", source="alertmanager", severity="info",
        labels={"namespace": "prod", "pod": "api-7f"}, raw_payload={},
    )
    alert_without_pod = Alert.from_parts(
        name="X", source="alertmanager", severity="info",
        labels={"namespace": "prod"}, raw_payload={},
    )

    args = {
        "namespace": "{{ alert.labels.namespace }}",
        "pod_name": "{{ alert.labels.pod }}",
        "query": "rate(http_requests_total{namespace='{{ alert.labels.namespace }}',pod='{{ alert.labels.pod }}'}[5m])",
    }

    rendered, missing = orch._render_args(args, alert_with_pod)
    assert missing == []
    assert rendered["namespace"] == "prod"
    assert rendered["pod_name"] == "api-7f"
    assert "pod='api-7f'" in rendered["query"]
    assert "pod=''" not in rendered["query"]

    rendered, missing = orch._render_args(args, alert_without_pod)
    assert missing, f"expected missing-label flags, got: {missing!r}"
    assert "query" not in rendered
    assert "pod_name" not in rendered
    assert rendered.get("namespace") == "prod"
    assert all("alert.labels.pod" in m for m in missing), (
        f"missing list should only contain pod-label refs, got: {missing}"
    )


def test_orchestrator_execute_action_preserves_structured_tool_output(monkeypatch):
    """Playbook execution must persist the real tool dict as Evidence.raw.

    The RCA builder reads fields like raw.events/raw.classification directly.
    A previous merge stringified every tool result into raw.output, which made
    final RCA prompts lose most of the useful pod/event/log structure.
    """
    import asyncio
    from alerts.domain.actions import InvestigationAction
    from alerts.orchestrator import engine as engine_mod
    from alerts.orchestrator.engine import InvestigationOrchestrator

    def fake_dispatch(tool_name, params, ctx):
        assert tool_name == "get_events"
        assert params == {"namespace": "jenkins-legacy"}
        assert ctx.surface == "playbook"
        return {
            "namespace": "jenkins-legacy",
            "events": [
                {
                    "type": "Warning",
                    "reason": "BackOff",
                    "message": "Back-off restarting failed container init",
                }
            ],
        }

    monkeypatch.setattr(engine_mod, "dispatch", fake_dispatch)
    orch = InvestigationOrchestrator.__new__(InvestigationOrchestrator)

    evidence = asyncio.run(
        orch._execute_action(
            InvestigationAction(
                tool="get_events",
                args={"namespace": "jenkins-legacy"},
                reason="collect events",
            )
        )
    )

    assert evidence.raw["namespace"] == "jenkins-legacy"
    assert evidence.raw["events"][0]["reason"] == "BackOff"
    assert "output" not in evidence.raw


def test_orchestrator_tracks_remaining_steps_by_tool_and_args():
    """The same MCP tool can be a valid separate RCA step with different args.

    Crashloop investigations need current logs and previous logs; Prometheus
    playbooks often need multiple different queries. Deduping only by tool
    name silently dropped those steps.
    """
    from alerts.domain.actions import ToolExecutionRecord
    from alerts.domain.alert import Alert
    from alerts.domain.investigation import Investigation
    from alerts.orchestrator.engine import InvestigationOrchestrator

    orch = InvestigationOrchestrator.__new__(InvestigationOrchestrator)
    alert = Alert.from_parts(
        name="KubernetesPodCrashLooping",
        source="manual",
        severity="critical",
        labels={"namespace": "prod", "pod": "api-0"},
        raw_payload={},
    )
    investigation = Investigation(alert=alert)
    current_args = {"namespace": "prod", "pod_name": "api-0"}
    previous_args = {"namespace": "prod", "pod_name": "api-0", "previous": True}
    investigation.executed_tools.append(
        ToolExecutionRecord(
            tool="get_pod_logs",
            arguments_hash="current",
            reason="current logs",
            args=current_args,
        )
    )

    remaining = orch._remaining_steps(
        [
            {"id": "current", "tool": "get_pod_logs", "args": current_args},
            {"id": "previous", "tool": "get_pod_logs", "args": previous_args},
            {"id": "events", "tool": "get_events", "args": {"namespace": "prod"}},
        ],
        investigation,
    )

    assert [step["id"] for step in remaining] == ["previous", "events"]


def test_prom_query_tool_registered_and_fails_soft_without_url(monkeypatch):
    """prom_query must be registered in the MCP tool registry so specialty
    playbooks (crashloopbackoff, highcpuusage, etc.) can dispatch it. When
    PROMETHEUS_URL is unset it must return {"unavailable": True, ...} rather
    than raise — that fail-soft contract is what lets investigations complete
    even when Prometheus is not configured / down."""
    monkeypatch.delenv("PROMETHEUS_URL", raising=False)
    from tool_registry import TOOLS
    assert "prom_query" in TOOLS, "prom_query tool was not registered"

    from services.prometheus import query as prom_query
    result = prom_query("up{job='kube-state-metrics'}")
    assert result.get("unavailable") is True
    assert "PROMETHEUS_URL not configured" in result.get("reason", "")


def test_append_messages_persists_to_session_history(monkeypatch):
    """The /api/sessions/{id}/messages endpoint persists user/assistant turns
    so client-side flows like /rca can write to history out-of-band and have
    the turns appear inline with regular chat — not bunched at the end via a
    client-side cache. This is the integration backing the /rca persistence
    path in the chat page."""
    monkeypatch.setenv("AUTH_ENABLED", "false")  # dev mode: no session ownership check
    client = TestClient(app)
    sid = "test-rca-persist"
    db.upsert_session(sid)

    resp = client.post(
        f"/api/sessions/{sid}/messages",
        json={
            "messages": [
                {"role": "user", "content": "/rca pod/foo"},
                {"role": "assistant", "content": "Triggered RCA. Investigation inv_abc."},
            ]
        },
    )
    assert resp.status_code == 200
    assert resp.json()["appended"] == 2

    # Confirm the turns appear in history in the order they were appended.
    hist = client.get(f"/api/sessions/{sid}/history")
    assert hist.status_code == 200
    msgs = hist.json().get("messages", [])
    # Last two messages should be our appended pair, in order.
    assert msgs[-2]["role"] == "user"
    assert msgs[-2]["content"] == "/rca pod/foo"
    assert msgs[-1]["role"] == "assistant"
    assert "inv_abc" in msgs[-1]["content"]

    # Bad role is rejected.
    bad = client.post(
        f"/api/sessions/{sid}/messages",
        json={"messages": [{"role": "system", "content": "nope"}]},
    )
    assert bad.status_code == 400


def test_manual_trigger_recalls_past_incidents(monkeypatch):
    """Phase 4b: after a first investigation persists to incident memory, a
    second similar investigation should recall it via semantic search and
    stamp a `similar_incidents_recalled` audit event with the past id."""
    import time
    import json
    monkeypatch.setenv("AUTH_ENABLED", "false")
    client = TestClient(app)

    # First investigation — populates incident memory.
    r1 = client.post("/api/v1/alerts/manual", json={"target": "prod/statefulset/api"})
    assert r1.status_code == 200
    inv1 = r1.json()["investigation_id"]
    for _ in range(10):
        with db._conn() as con:
            row = con.execute("SELECT status FROM investigations WHERE id=?", (inv1,)).fetchone()
            if row and row["status"] in ("completed", "failed"):
                break
        time.sleep(1)

    # Second investigation, similar target — should recall the first.
    r2 = client.post("/api/v1/alerts/manual", json={"target": "prod/statefulset/payments"})
    assert r2.status_code == 200
    inv2 = r2.json()["investigation_id"]
    for _ in range(10):
        with db._conn() as con:
            row = con.execute("SELECT status,document FROM investigations WHERE id=?", (inv2,)).fetchone()
            if row and row["status"] in ("completed", "failed"):
                break
        time.sleep(1)
    assert row["status"] == "completed"

    doc = json.loads(row["document"])
    audit_log = doc.get("audit_log", [])
    recall_events = [e for e in audit_log if e.get("event_type") == "similar_incidents_recalled"]
    assert len(recall_events) == 1, (
        f"expected a similar_incidents_recalled audit event in the second investigation; "
        f"got event types: {[e.get('event_type') for e in audit_log]}"
    )
    payload = recall_events[0]["payload"]
    assert payload["count"] >= 1
    assert inv1 in payload["investigation_ids"], (
        f"expected the first investigation_id {inv1} to be recalled; got {payload['investigation_ids']}"
    )
    # Self-exclusion: the current investigation must not be in its own recall.
    assert inv2 not in payload["investigation_ids"]


@pytest.mark.parametrize("target, expected_ns, expected_kind, expected_name, expected_alertname", [
    ("frontend", "default", "pod", "frontend", "ManualPodInvestigation"),
    ("pod/frontend", "default", "pod", "frontend", "ManualPodInvestigation"),
    ("prod/pod/frontend", "prod", "pod", "frontend", "ManualPodInvestigation"),
    ("prod/deployment/api", "prod", "deployment", "api", "ManualWorkloadInvestigation"),
])
def test_manual_trigger_grammar(monkeypatch, target, expected_ns, expected_kind, expected_name, expected_alertname):
    from fastapi import Request
    from starlette.requests import Request as StarletteRequest
    from unittest.mock import Mock, patch
    
    monkeypatch.setenv("AUTH_ENABLED", "true")
    
    # We mock Request and BackgroundTasks so we can test the handler directly
    # since TestClient bypassing auth middleware is tricky.
    req_mock = Mock(spec=StarletteRequest)
    req_mock.state.user = {"id": "test_user_123"}
    bg_tasks_mock = Mock()
    
    import asyncio
    req = alerts_router.ManualInvestigationRequest(target=target)
    
    resp = asyncio.run(
        alerts_router.trigger_manual_investigation(req, req_mock, bg_tasks_mock)
    )
    assert resp.investigation_id is not None
    
    # Validate the stored investigation
    with db._conn() as con:
        row = con.execute("SELECT * FROM investigations WHERE id = ?", (resp.investigation_id,)).fetchone()
    
    assert row is not None
    import json
    doc = json.loads(row["document"])
    
    # Assert Grammar
    alert = doc.get("alert", {})
    assert alert.get("source") == "manual"
    labels = alert.get("labels", {})
    assert labels.get("namespace") == expected_ns
    assert labels.get("kind") == expected_kind
    assert labels.get("resource") == expected_name
    assert alert.get("name") == expected_alertname
    
    # Assert Audit Trail
    audit_log = doc.get("audit_log", [])
    assert len(audit_log) > 0
    
    manual_trigger_event = next((e for e in audit_log if e["event_type"] == "manual_trigger"), None)
    assert manual_trigger_event is not None, "manual_trigger event not found in audit log"
    assert manual_trigger_event["payload"]["user_id"] == "test_user_123"

def test_manual_trigger_routing():
    # We test that the classifier rules correctly map the manual alerts to the correct playbooks
    mcp_path = alerts_router._resolve_mcp_path()
    playbook_path = alerts_router._resolve_playbook_path()
    
    from alerts.playbooks.registry import PlaybookRegistry
    from alerts.playbooks.loader import PlaybookLoader
    from alerts.playbooks.classifier import AlertClassifier
    from alerts.domain.alert import Alert
    
    playbooks = PlaybookRegistry(PlaybookLoader(playbook_path))
    classifier = AlertClassifier(playbooks.list(), rules_path=os.path.join(playbook_path, "classification-rules.yaml"))
    
    pod_alert = Alert.from_parts(name="ManualPodInvestigation", source="manual", severity="info", labels={}, raw_payload={})
    workload_alert = Alert.from_parts(name="ManualWorkloadInvestigation", source="manual", severity="info", labels={}, raw_payload={})
    
    pod_classification = classifier.classify(pod_alert)
    assert pod_classification.playbook_id == "generic-pod"
    assert pod_classification.confidence == 0.99
    
    workload_classification = classifier.classify(workload_alert)
    assert workload_classification.playbook_id == "generic-workload"
    assert workload_classification.confidence == 0.99
