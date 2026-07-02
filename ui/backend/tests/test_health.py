from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import db
from main import app
from routers import health


@pytest.fixture(autouse=True)
def _reset_kubectl_cache():
    # The kubectl diagnostic is cached in-process for 5s. Reset between tests
    # so monkeypatched subprocess.run is actually invoked each time.
    health._kubectl_cache = None
    yield
    health._kubectl_cache = None


def test_liveness_is_dependency_free(monkeypatch):
    monkeypatch.setattr(
        health.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("subprocess called")),
    )
    client = TestClient(app)
    assert client.get("/health/live").json() == {"status": "ok"}


def test_readiness_returns_503_before_initialization(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "health.db"))
    db.init_db()
    health.set_initialization_state(initialized=False, configuration_valid=True)
    response = TestClient(app).get("/health/ready")
    # TestClient runs lifespan and normally makes this ready, so exercise the
    # readiness function directly for the pre-initialization contract.
    health.set_initialization_state(initialized=False, configuration_valid=True)
    import asyncio
    direct = asyncio.run(health.readiness())
    assert direct.status_code == 503
    assert response.status_code in (200, 503)


def test_kubectl_in_cluster_without_named_context(monkeypatch):
    monkeypatch.setattr(health.shutil, "which", lambda name: "/usr/bin/kubectl")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setattr(
        health.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="{}", stderr=""),
    )
    available, context, mode, check = health._kubectl_check()
    assert available is True
    assert context == "in-cluster"
    assert mode == "in_cluster"
    assert check.status == "ok"


def test_kubectl_timeout_is_unavailable(monkeypatch):
    import subprocess

    monkeypatch.setattr(health.shutil, "which", lambda name: "/usr/bin/kubectl")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("kubectl", 4)

    monkeypatch.setattr(health.subprocess, "run", timeout)
    available, context, mode, check = health._kubectl_check()
    assert available is False
    assert context is None
    assert mode == "unavailable"
    assert check.status == "failed"
