"""Live cluster counters and topology.

The load-bearing behaviours:

  * a health colour that reflects what the workload was *told* to do, not just
    what it is doing — DaemonSets have no `spec.replicas`, and a workload
    scaled to zero is not broken;
  * caching and single-flight, because these are polled every 30s per open tab
    and each miss is a `kubectl` subprocess;
  * "no cluster" and "no permission" rendering as states rather than errors.

The design doc's versions of the first two were wrong in ways that produce a
map covered in red boxes, so they are pinned here.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import cluster_summary as summary_mod  # noqa: E402
import cluster_topology as topology_mod  # noqa: E402

CONN = {
    "cluster_name": "prod",
    "context_name": "prod-ctx",
    "namespace": "payments",
    "kubeconfig_path": None,
}


class FakeRunner:
    """Stands in for KubectlRunner, recording what was asked of it."""

    def __init__(self, responses: dict[str, dict], can_i: str = "yes"):
        self.responses = responses
        self.can_i = can_i
        self.calls: list[list[str]] = []

    def run(self, args, **kwargs):
        self.calls.append(list(args))

        class Result:
            pass

        result = Result()
        if args[:2] == ["auth", "can-i"]:
            result.stdout = self.can_i
            return result

        kind = args[1]
        result.stdout = json.dumps(self.responses.get(kind, {"items": []}))
        return result


def _install_runner(monkeypatch, module, runner):
    """Patch the KubectlRunner these modules import inside their fetch path."""
    import k8s.kubectl_runner as kr

    monkeypatch.setattr(kr, "KubectlRunner", lambda **kwargs: runner)


def _pod(ready: bool):
    return {
        "status": {
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]
        }
    }


# ── health colours ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ready, desired, expected",
    [
        (3, 3, "green"),
        (5, 3, "green"),  # over-provisioned during a rollout is not a problem
        (1, 3, "amber"),
        (0, 3, "red"),
        (0, 0, "idle"),
    ],
)
def test_health_colour(ready, desired, expected):
    assert topology_mod._health(ready, desired) == expected


def test_a_workload_scaled_to_zero_is_not_red():
    """It is doing exactly what it was told.

    A naive `ready == 0 -> red` puts a permanent alarm on the map for
    something nobody needs to fix, and the map's whole job is to show the
    things that do.
    """
    assert topology_mod._health(0, 0) == "idle"
    assert topology_mod._health(0, 1) == "red"


def test_daemonsets_read_their_own_replica_fields():
    """DaemonSets have no `spec.replicas` — the node count decides.

    Reading `replicas` for one yields 0 desired, so every DaemonSet in the
    cluster renders as `idle` (or, before that fix, red) no matter its state.
    """
    daemonset = {
        "status": {"numberReady": 4, "desiredNumberScheduled": 5},
        "spec": {},
    }

    assert topology_mod._replica_counts(daemonset, "DaemonSet") == (4, 5)


def test_a_deployment_without_replicas_wants_one():
    """`spec.replicas` is optional and defaults to 1.

    Treating a missing value as 0 makes an unset Deployment report `idle`
    regardless of whether its pod is running.
    """
    assert topology_mod._replica_counts({"status": {}}, "Deployment") == (0, 1)


# ── topology ──────────────────────────────────────────────────────────────


def _topology_payload():
    return {
        "items": [
            {
                "kind": "Deployment",
                "metadata": {"name": "api", "namespace": "payments"},
                "spec": {"replicas": 3},
                "status": {"readyReplicas": 3},
            },
            {
                "kind": "Deployment",
                "metadata": {"name": "worker", "namespace": "payments"},
                "spec": {"replicas": 2},
                "status": {"readyReplicas": 0},
            },
            {
                "kind": "DaemonSet",
                "metadata": {"name": "log-shipper", "namespace": "payments"},
                "spec": {},
                "status": {"numberReady": 4, "desiredNumberScheduled": 5},
            },
        ]
    }


def test_alerting_scope_hides_healthy_workloads(monkeypatch):
    runner = FakeRunner({topology_mod.WORKLOAD_KINDS: _topology_payload()})
    _install_runner(monkeypatch, topology_mod, runner)

    result = topology_mod.ClusterTopologyService()._fetch(CONN, "alerting")

    names = {n["name"]: n["health"] for n in result.nodes}
    assert names == {"worker": "red", "log-shipper": "amber"}


def test_all_scope_keeps_everything(monkeypatch):
    runner = FakeRunner({topology_mod.WORKLOAD_KINDS: _topology_payload()})
    _install_runner(monkeypatch, topology_mod, runner)

    result = topology_mod.ClusterTopologyService()._fetch(CONN, "all")

    assert len(result.nodes) == 3
    assert {n["name"] for n in result.nodes} == {"api", "worker", "log-shipper"}


def test_node_ids_distinguish_kinds(monkeypatch):
    """A Deployment and a StatefulSet may share a name.

    Keying on namespace/name alone collapses them into one node and drops an
    edge, which is the kind of wrong that looks plausible on screen.
    """
    payload = {
        "items": [
            {
                "kind": "Deployment",
                "metadata": {"name": "cache", "namespace": "payments"},
                "spec": {"replicas": 1},
                "status": {"readyReplicas": 0},
            },
            {
                "kind": "StatefulSet",
                "metadata": {"name": "cache", "namespace": "payments"},
                "spec": {"replicas": 1},
                "status": {"readyReplicas": 0},
            },
        ]
    }
    runner = FakeRunner({topology_mod.WORKLOAD_KINDS: payload})
    _install_runner(monkeypatch, topology_mod, runner)

    result = topology_mod.ClusterTopologyService()._fetch(CONN, "all")

    assert len({n["id"] for n in result.nodes}) == 2


def test_traffic_edges_are_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("PROM_TRAFFIC_EDGES", raising=False)

    assert topology_mod._traffic_edges({"payments/Deployment/api"}) == []


def test_unparseable_kubectl_output_yields_no_nodes(monkeypatch):
    """The runner caps output size, so truncation is an expected failure.

    A partial graph is worse than an empty one: it looks complete.
    """
    class Broken(FakeRunner):
        def run(self, args, **kwargs):
            class Result:
                stdout = '{"items": [{"kind": "Deploy'
            return Result() if args[:2] != ["auth", "can-i"] else super().run(args)

    _install_runner(monkeypatch, topology_mod, Broken({}))

    assert topology_mod.ClusterTopologyService()._fetch(CONN, "all").nodes == []


# ── summary counters ──────────────────────────────────────────────────────


def test_counters_come_from_the_cluster(monkeypatch):
    runner = FakeRunner(
        {
            "pods": {"items": [_pod(True), _pod(True), _pod(False)]},
            "deployments": {
                "items": [
                    {"spec": {"replicas": 2}, "status": {"readyReplicas": 2}},
                    {"spec": {"replicas": 2}, "status": {"readyReplicas": 1}},
                ]
            },
        }
    )
    _install_runner(monkeypatch, summary_mod, runner)
    monkeypatch.setattr(summary_mod, "_count_alerts", lambda ns: (0, 0))

    result = summary_mod.ClusterSummaryService()._fetch(CONN)

    assert result.counters["pods_total"] == 3
    assert result.counters["pods_ready"] == 2
    assert result.counters["workloads_degraded"] == 1
    assert result.namespace == "payments"


def test_a_denied_credential_raises_rbac_not_a_failure(monkeypatch):
    """`auth can-i` answering "no" is information, not an outage."""
    runner = FakeRunner({}, can_i="no")
    _install_runner(monkeypatch, summary_mod, runner)

    with pytest.raises(summary_mod.RBACError):
        summary_mod.ClusterSummaryService()._fetch(CONN)


def test_the_rbac_check_runs_before_anything_is_listed(monkeypatch):
    """Order matters: without the preflight, a denial surfaces as a kubectl
    failure and the UI cannot tell it apart from the cluster being down."""
    runner = FakeRunner({}, can_i="no")
    _install_runner(monkeypatch, summary_mod, runner)

    with pytest.raises(summary_mod.RBACError):
        summary_mod.ClusterSummaryService()._fetch(CONN)

    assert runner.calls == [["auth", "can-i", "list", "pods", "-n", "payments"]]


# ── alert counters, from the local investigations table ───────────────────


@pytest.fixture
def investigations(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    import db

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"), raising=False)
    db.init_db()
    with db._conn() as con:
        con.execute("DELETE FROM investigations")
    yield db
    with db._conn() as con:
        con.execute("DELETE FROM investigations")


def _insert(db_module, namespace, severity, age_hours=0):
    when = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with db_module._conn() as con:
        con.execute(
            "INSERT INTO investigations (id, namespace, severity, source, status, "
            "created_at, document) VALUES (?, ?, ?, 'test', 'done', ?, '{}')",
            (f"{namespace}-{severity}-{age_hours}-{id(when)}", namespace, severity, when),
        )


def test_alert_counts_are_scoped_to_the_namespace(investigations):
    """A header reading "12 pods / 40 alerts" where the alerts are
    cluster-wide invites the reader to connect two unrelated numbers."""
    _insert(investigations, "payments", "warning")
    _insert(investigations, "payments", "critical")
    _insert(investigations, "other-team", "critical")

    active, sev1 = summary_mod._count_alerts("payments")

    assert (active, sev1) == (2, 1)


def test_old_alerts_fall_out_of_the_active_window(investigations):
    _insert(investigations, "payments", "critical", age_hours=1)
    _insert(investigations, "payments", "critical", age_hours=48)

    active, sev1 = summary_mod._count_alerts("payments")

    assert (active, sev1) == (1, 1)


@pytest.mark.parametrize("severity", ["critical", "sev1", "sev-1", "P1", "Critical"])
def test_sev1_spellings_all_count(investigations, severity):
    """Alertmanager sends `critical`; Grafana and in-house routers do not."""
    _insert(investigations, "payments", severity)

    assert summary_mod._count_alerts("payments")[1] == 1


def test_a_broken_database_does_not_take_down_the_header(monkeypatch):
    """The pod counters beside these are still worth showing."""
    import db

    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "_conn", boom)

    assert summary_mod._count_alerts("payments") == (0, 0)


# ── caching and single-flight ─────────────────────────────────────────────


def test_a_second_call_inside_the_ttl_does_not_hit_the_cluster(monkeypatch):
    runner = FakeRunner({"pods": {"items": []}, "deployments": {"items": []}})
    _install_runner(monkeypatch, summary_mod, runner)
    monkeypatch.setattr(summary_mod, "_count_alerts", lambda ns: (0, 0))
    service = summary_mod.ClusterSummaryService(cache_ttl_s=30)

    async def scenario():
        first = await service.get("sess-1", CONN)
        second = await service.get("sess-1", CONN)
        return first, second

    first, second = asyncio.run(scenario())

    assert first.generated_at == second.generated_at
    assert len([c for c in runner.calls if c[:1] == ["get"]]) == 2  # pods + deploys


def test_concurrent_callers_coalesce_onto_one_fetch(monkeypatch):
    """Ten tabs on one session must not become ten kubectl subprocesses."""
    runner = FakeRunner({"pods": {"items": []}, "deployments": {"items": []}})
    _install_runner(monkeypatch, summary_mod, runner)
    monkeypatch.setattr(summary_mod, "_count_alerts", lambda ns: (0, 0))
    service = summary_mod.ClusterSummaryService(cache_ttl_s=30)

    async def scenario():
        return await asyncio.gather(*(service.get("sess-1", CONN) for _ in range(10)))

    results = asyncio.run(scenario())

    assert len({r.generated_at for r in results}) == 1
    assert len([c for c in runner.calls if c[:1] == ["get"]]) == 2


def test_cache_age_is_reported_not_hidden(monkeypatch):
    """A stale number presented as live is worse than a stale number labelled."""
    runner = FakeRunner({"pods": {"items": []}, "deployments": {"items": []}})
    _install_runner(monkeypatch, summary_mod, runner)
    monkeypatch.setattr(summary_mod, "_count_alerts", lambda ns: (0, 0))
    service = summary_mod.ClusterSummaryService(cache_ttl_s=30)

    async def scenario():
        await service.get("sess-1", CONN)
        # Rewind the cache timestamp rather than sleeping.
        stamp, value = service._cache["sess-1"]
        service._cache["sess-1"] = (stamp - 12, value)
        return await service.get("sess-1", CONN)

    assert asyncio.run(scenario()).cache_age_seconds >= 12


def test_sessions_do_not_share_a_cache_entry(monkeypatch):
    """Two users on two clusters must not see each other's counters."""
    runner = FakeRunner({"pods": {"items": [_pod(True)]}, "deployments": {"items": []}})
    _install_runner(monkeypatch, summary_mod, runner)
    monkeypatch.setattr(summary_mod, "_count_alerts", lambda ns: (0, 0))
    service = summary_mod.ClusterSummaryService(cache_ttl_s=30)

    async def scenario():
        await service.get("sess-a", CONN)
        await service.get("sess-b", {**CONN, "namespace": "other"})

    asyncio.run(scenario())

    assert service._cache["sess-a"][1].namespace == "payments"
    assert service._cache["sess-b"][1].namespace == "other"


def test_invalidate_forces_the_next_call_to_refetch(monkeypatch):
    """Switching cluster must not leave the old cluster's numbers on screen."""
    runner = FakeRunner({"pods": {"items": []}, "deployments": {"items": []}})
    _install_runner(monkeypatch, summary_mod, runner)
    monkeypatch.setattr(summary_mod, "_count_alerts", lambda ns: (0, 0))
    service = summary_mod.ClusterSummaryService(cache_ttl_s=30)

    async def scenario():
        await service.get("sess-1", CONN)
        service.invalidate("sess-1")
        await service.get("sess-1", CONN)

    asyncio.run(scenario())

    assert len([c for c in runner.calls if c[:1] == ["get"]]) == 4


# ── the endpoints ─────────────────────────────────────────────────────────
#
# What matters here is the status code. "No cluster connected" and "your
# credential cannot list pods" are both states the header renders; answering
# them with 4xx would make an uninformed panel look like a broken one.


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import auth
    from routers import cluster_live

    monkeypatch.setattr(auth, "require_owned_session", lambda request, sid: None)
    monkeypatch.setattr(cluster_live.auth, "require_owned_session", lambda request, sid: None)

    app = FastAPI()
    app.include_router(cluster_live.router, prefix="/api")
    return TestClient(app)


def test_no_cluster_is_a_state_not_an_error(client, monkeypatch):
    from routers import cluster_live

    monkeypatch.setattr(cluster_live.db, "get_cluster_connection", lambda sid: None)

    response = client.get("/api/v1/cluster/summary/sess-1")

    assert response.status_code == 200
    assert response.json()["reason"] == "no_cluster"
    assert response.json()["counters"] is None


def test_insufficient_rbac_is_a_state_not_an_error(client, monkeypatch):
    from routers import cluster_live

    monkeypatch.setattr(cluster_live.db, "get_cluster_connection", lambda sid: CONN)

    async def denied(session_id, conn):
        raise cluster_live.RBACError("nope")

    monkeypatch.setattr(cluster_live._summary, "get", denied)

    response = client.get("/api/v1/cluster/summary/sess-1")

    assert response.status_code == 200
    body = response.json()
    assert body["reason"] == "insufficient_rbac"
    # The connection is still described, so the header can name the cluster
    # it cannot read.
    assert body["cluster"] == "prod"
    assert body["counters"] is None


def test_a_real_failure_is_still_a_failure(client, monkeypatch):
    """Distinct from the two above: something broke, and it should say so."""
    from routers import cluster_live

    monkeypatch.setattr(cluster_live.db, "get_cluster_connection", lambda sid: CONN)

    async def boom(session_id, conn):
        raise RuntimeError("kubectl exploded")

    monkeypatch.setattr(cluster_live._summary, "get", boom)

    assert client.get("/api/v1/cluster/summary/sess-1").status_code == 500


def test_topology_rejects_an_out_of_range_depth(client, monkeypatch):
    from routers import cluster_live

    monkeypatch.setattr(cluster_live.db, "get_cluster_connection", lambda sid: CONN)

    assert client.get("/api/v1/cluster/topology/sess-1?depth=9").status_code == 400


def test_topology_without_a_cluster_is_an_empty_graph(client, monkeypatch):
    from routers import cluster_live

    monkeypatch.setattr(cluster_live.db, "get_cluster_connection", lambda sid: None)

    response = client.get("/api/v1/cluster/topology/sess-1")

    assert response.status_code == 200
    assert response.json() == {"nodes": [], "edges": [], "generated_at": ""}


def test_the_endpoints_require_an_owned_session(monkeypatch):
    """The guard is the only thing stopping session ids being enumerated for
    other people's cluster counters."""
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from routers import cluster_live

    def refuse(request, sid):
        raise HTTPException(status_code=403, detail="not yours")

    monkeypatch.setattr(cluster_live.auth, "require_owned_session", refuse)

    app = FastAPI()
    app.include_router(cluster_live.router, prefix="/api")
    client = TestClient(app)

    assert client.get("/api/v1/cluster/summary/someone-else").status_code == 403
    assert client.get("/api/v1/cluster/topology/someone-else").status_code == 403
