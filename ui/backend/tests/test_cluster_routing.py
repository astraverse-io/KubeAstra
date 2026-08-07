"""Which cluster is this alert about?

One assistant, several clusters, each with its own Prometheus pointing at it.
Before routing, every alert was investigated against whatever target the
backend happened to be aimed at — so an alert from staging produced a
confident, fully-evidenced root-cause answer about production.

That is worse than no answer. The evidence is real, the reasoning is sound, and
it is about a different machine — nothing about it looks wrong. So the rule is:
investigate the right cluster, or investigate nothing and say why.

The other half is not regressing the deployments that have one cluster and no
labels, which is every existing one.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import cluster_routing as routing  # noqa: E402
import db  # noqa: E402

PROD = {"id": "prod", "ssh_host": "h", "status": "active"}
REGISTRY = {"prod": PROD, "disabled-one": {**PROD, "id": "disabled-one", "status": "disabled"}}


def _lookup(cluster_id: str):
    return REGISTRY.get(cluster_id)


# ── single-cluster mode must not change ───────────────────────────────────


def test_an_empty_registry_always_uses_the_default_target():
    """Every existing deployment is here. Nothing about it may change."""
    route = routing.resolve({}, registry_is_empty=True, lookup=_lookup)

    assert route.investigate
    assert route.cluster is None


def test_an_empty_registry_ignores_a_cluster_label_entirely():
    """Half-honouring it would make an unregistered label start failing alerts
    that work today — a regression triggered by a label nobody asked for."""
    route = routing.resolve(
        {"cluster": "never-registered"}, registry_is_empty=True, lookup=_lookup
    )

    assert route.investigate


# ── routing to a registered cluster ───────────────────────────────────────


def test_a_registered_cluster_is_routed_to():
    route = routing.resolve(
        {"cluster": "prod"}, registry_is_empty=False, lookup=_lookup
    )

    assert route.investigate
    assert route.cluster_id == "prod"


# ── refusing, rather than guessing ────────────────────────────────────────


def test_an_unlabelled_alert_is_not_investigated_in_multi_cluster_mode():
    """There is no safe default. Picking one means a confident answer about the
    wrong machine."""
    route = routing.resolve({}, registry_is_empty=False, lookup=_lookup)

    assert not route.investigate
    assert "external_labels" in route.reason  # tells the operator the fix


def test_an_unregistered_cluster_is_not_investigated():
    route = routing.resolve(
        {"cluster": "prod-eu"}, registry_is_empty=False, lookup=_lookup
    )

    assert not route.investigate
    assert "prod-eu" in route.reason


def test_a_disabled_cluster_does_not_fall_back_to_the_default_target():
    """Disabling is deliberate, so this is not an error — but falling back
    would send the investigation to a machine nobody chose."""
    route = routing.resolve(
        {"cluster": "disabled-one"}, registry_is_empty=False, lookup=_lookup
    )

    assert not route.investigate
    assert route.cluster is None


def test_matching_is_exact():
    """No normalisation. Deciding that `PROD` and `prod` are the same thing is
    how an alert reaches the wrong machine."""
    assert not routing.resolve(
        {"cluster": "PROD"}, registry_is_empty=False, lookup=_lookup
    ).investigate


def test_a_whitespace_only_label_counts_as_absent():
    assert not routing.resolve(
        {"cluster": "   "}, registry_is_empty=False, lookup=_lookup
    ).investigate


# ── manual runs ───────────────────────────────────────────────────────────


def test_a_manual_run_still_uses_the_default_target():
    """A manual investigation is started by a person against the cluster the
    backend already points at, and carries no `cluster` label because there is
    nothing to carry one.

    Without this, registering your first cluster would silently break every
    manual investigation.
    """
    route = routing.resolve(
        {}, registry_is_empty=False, lookup=_lookup, is_manual=True
    )

    assert route.investigate
    assert route.cluster is None


def test_a_manual_run_that_names_a_cluster_still_routes():
    route = routing.resolve(
        {"cluster": "prod"}, registry_is_empty=False, lookup=_lookup, is_manual=True
    )

    assert route.cluster_id == "prod"


# ── the registry ──────────────────────────────────────────────────────────


@pytest.fixture
def clean_db():
    db.init_db()
    with db._conn() as con:
        con.execute("DELETE FROM cluster_registry")
        con.execute("DELETE FROM investigations")
    yield db
    with db._conn() as con:
        con.execute("DELETE FROM cluster_registry")
        con.execute("DELETE FROM investigations")


def test_registering_and_reading_back(clean_db):
    db.register_cluster("prod", "master.example", "kubeastra", "cluster-ssh")

    stored = db.get_cluster("prod")
    assert stored["ssh_host"] == "master.example"
    assert stored["status"] == "active"


def test_no_credential_material_is_stored(clean_db):
    """Only the name of a mounted secret. A database dump must not hand over
    cluster access."""
    db.register_cluster("prod", "master.example", "kubeastra", "cluster-ssh")

    columns = set(db.get_cluster("prod"))
    assert not columns & {"ssh_key", "ssh_password", "private_key", "credential"}
    assert db.get_cluster("prod")["credential_ref"] == "cluster-ssh"


def test_registering_the_same_id_updates_rather_than_duplicates(clean_db):
    db.register_cluster("prod", "old.example", "kubeastra", "cluster-ssh")
    db.register_cluster("prod", "new.example", "kubeastra", "cluster-ssh")

    assert db.get_cluster("prod")["ssh_host"] == "new.example"
    assert len(db.list_clusters()) == 1


def test_an_invalid_status_is_refused(clean_db):
    with pytest.raises(ValueError):
        db.register_cluster("prod", "h", "u", "s", status="maybe")


def test_the_registry_reports_empty_until_something_is_registered(clean_db):
    assert db.registry_is_empty() is True
    db.register_cluster("prod", "h", "u", "s")
    assert db.registry_is_empty() is False


def test_disabling_every_cluster_does_not_return_to_single_cluster_mode(clean_db):
    """Otherwise disabling clusters would silently send all alerts back to the
    default target — the one outcome nobody intends by disabling things."""
    db.register_cluster("prod", "h", "u", "s", status="disabled")

    assert db.registry_is_empty() is False


def test_removing_the_last_cluster_returns_to_single_cluster_mode(clean_db):
    db.register_cluster("prod", "h", "u", "s")
    assert db.remove_cluster("prod") is True
    assert db.registry_is_empty() is True


# ── the webhook path ──────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch, clean_db):
    from fastapi.testclient import TestClient

    from main import app
    from routers import alerts as alerts_router

    monkeypatch.setenv("ALERTMANAGER_WEBHOOK_ENABLED", "true")
    monkeypatch.delenv("ALERT_WEBHOOK_TOKEN", raising=False)
    alerts_router.reset_webhook_settings()
    monkeypatch.setattr(
        alerts_router, "orchestrate_investigation", lambda *a, **k: None
    )
    return TestClient(app)


def _payload(cluster: str | None = None, name: str = "HighCPU"):
    labels = {"alertname": name, "severity": "critical", "namespace": "demo"}
    if cluster is not None:
        labels["cluster"] = cluster
    return {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "fingerprint": f"fp-{name}-{cluster}",
                "startsAt": "2026-08-07T12:00:00Z",
                "labels": labels,
                "annotations": {"description": "high"},
            }
        ],
    }


def _status(investigation_id: str):
    with db._conn() as con:
        return con.execute(
            "SELECT status, status_reason FROM investigations WHERE id = ?",
            (investigation_id,),
        ).fetchone()


def test_with_no_registry_an_unlabelled_alert_is_investigated(client):
    """The no-regression case, exercised end to end."""
    response = client.post("/api/v1/alerts/webhook", json=_payload()).json()

    assert response["unroutable"] == 0
    assert _status(response["investigation_ids"][0])["status"] == "received"


def test_an_alert_for_a_registered_cluster_is_investigated(client):
    db.register_cluster("prod", "master.example", "kubeastra", "cluster-ssh")

    response = client.post("/api/v1/alerts/webhook", json=_payload("prod")).json()

    assert response["unroutable"] == 0


def test_an_alert_for_an_unknown_cluster_is_recorded_but_not_investigated(client):
    """The investigation exists so the operator sees the alert. The point is
    that nothing was investigated, not that nothing happened."""
    db.register_cluster("prod", "master.example", "kubeastra", "cluster-ssh")

    response = client.post("/api/v1/alerts/webhook", json=_payload("prod-eu")).json()

    assert response["unroutable"] == 1
    row = _status(response["investigation_ids"][0])
    assert row["status"] == "needs_config"
    assert "prod-eu" in row["status_reason"]


def test_the_reason_says_how_to_fix_it(client):
    """"needs_config" alone tells an operator nothing. The row has to name the
    missing piece."""
    db.register_cluster("prod", "master.example", "kubeastra", "cluster-ssh")

    response = client.post("/api/v1/alerts/webhook", json=_payload()).json()

    assert "external_labels" in _status(response["investigation_ids"][0])["status_reason"]


def test_a_needs_config_investigation_does_not_wedge_its_incident_open(clean_db):
    """`needs_config` is terminal. Left out of the terminal set it would sit
    "open" forever, absorb every repeat of that alert, and hold its incident
    open permanently."""
    assert "needs_config" in db.TERMINAL_INVESTIGATION_STATUSES
    assert "needs_config" not in db.OPEN_INVESTIGATION_STATUSES


def test_a_refire_after_registering_the_cluster_is_investigated(client):
    """Routing repairs itself: nothing has to reprocess a backlog, because
    needs_config is terminal and the next delivery starts fresh."""
    db.register_cluster("prod", "master.example", "kubeastra", "cluster-ssh")
    first = client.post("/api/v1/alerts/webhook", json=_payload("prod-eu")).json()

    db.register_cluster("prod-eu", "eu.example", "kubeastra", "cluster-ssh")
    second = client.post("/api/v1/alerts/webhook", json=_payload("prod-eu")).json()

    assert second["investigation_ids"] != first["investigation_ids"]
    assert second["unroutable"] == 0


def test_an_unreadable_registry_falls_back_to_investigating(client, monkeypatch):
    """A registry failure must not stop alerting outright — degrade to
    single-cluster rather than refusing everything."""
    from routers import alerts as alerts_router

    def boom():
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(alerts_router.db, "registry_is_empty", boom)

    response = client.post("/api/v1/alerts/webhook", json=_payload()).json()

    assert response["unroutable"] == 0
    assert len(response["investigation_ids"]) == 1


# ── the admin API ─────────────────────────────────────────────────────────


def test_registering_through_the_api(client):
    response = client.post(
        "/api/v1/clusters",
        json={
            "id": "prod",
            "ssh_host": "master.example",
            "ssh_user": "kubeastra",
            "credential_ref": "cluster-ssh",
        },
    )

    assert response.status_code == 201
    assert response.json()["cluster"]["id"] == "prod"


def test_the_first_registration_announces_the_mode_switch(client):
    """Registering the first cluster stops unlabelled alerts being
    investigated. That is a bigger change than adding a row looks."""
    first = client.post(
        "/api/v1/clusters",
        json={"id": "a", "ssh_host": "h", "ssh_user": "u", "credential_ref": "s"},
    ).json()
    second = client.post(
        "/api/v1/clusters",
        json={"id": "b", "ssh_host": "h", "ssh_user": "u", "credential_ref": "s"},
    ).json()

    assert first["switched_to_multi_cluster"] is True
    assert second["switched_to_multi_cluster"] is False


def test_listing_reports_which_mode_is_active(client):
    assert client.get("/api/v1/clusters").json()["multi_cluster"] is False
    db.register_cluster("prod", "h", "u", "s")
    assert client.get("/api/v1/clusters").json()["multi_cluster"] is True


def test_removing_the_last_cluster_is_announced(client):
    client.post(
        "/api/v1/clusters",
        json={"id": "a", "ssh_host": "h", "ssh_user": "u", "credential_ref": "s"},
    )

    assert client.delete("/api/v1/clusters/a").json()["back_to_single_cluster"] is True


def test_removing_an_unknown_cluster_is_a_404(client):
    assert client.delete("/api/v1/clusters/nope").status_code == 404


def test_a_bad_port_is_refused(client):
    response = client.post(
        "/api/v1/clusters",
        json={
            "id": "a",
            "ssh_host": "h",
            "ssh_user": "u",
            "credential_ref": "s",
            "ssh_port": 99999,
        },
    )

    assert response.status_code == 422
