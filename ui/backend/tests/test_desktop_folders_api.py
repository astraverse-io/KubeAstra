"""Endpoint tests for /api/desktop/folders/* (Phase 1, PR 4).

Grant → list → revoke round-trip, server-side re-validation (bad mode, missing
dir, sensitive root refused), and the folder.grant / folder.revoke audit events.
Uses a bare app like the other desktop router tests (the desktop_security token
boundary is covered separately); these focus on router behavior.
"""

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from routers import desktop_folders as folders_router  # noqa: E402


@pytest.fixture
def audit_events(monkeypatch):
    import audit
    events = []
    monkeypatch.setattr(audit, "emit", lambda et, **kw: events.append((et, kw)) or "evt")
    return events


@pytest.fixture
def client(tmp_path, monkeypatch):
    import desktop_paths
    monkeypatch.setattr(desktop_paths, "config_path", lambda: tmp_path / "desktop_config.json")
    app = FastAPI()
    app.include_router(folders_router.router, prefix="/api")
    return TestClient(app)


def test_grant_list_revoke_roundtrip(client, tmp_path, audit_events):
    import audit
    root = tmp_path / "infra"
    root.mkdir()

    r = client.post("/api/desktop/folders/grant", json={"root": str(root), "mode": "read"})
    assert r.status_code == 200
    grant = r.json()
    assert grant["mode"] == "read" and grant["id"].startswith("grt_")

    listed = client.get("/api/desktop/folders/grants").json()["grants"]
    assert any(g["id"] == grant["id"] for g in listed)

    d = client.delete(f"/api/desktop/folders/grant/{grant['id']}")
    assert d.status_code == 200 and d.json()["revoked"] is True
    assert client.get("/api/desktop/folders/grants").json()["grants"] == []

    types = [et for et, _ in audit_events]
    assert audit.EventType.FOLDER_GRANT in types
    assert audit.EventType.FOLDER_REVOKE in types


def test_invalid_mode_rejected(client, tmp_path, audit_events):
    root = tmp_path / "infra"
    root.mkdir()
    r = client.post("/api/desktop/folders/grant", json={"root": str(root), "mode": "sudo"})
    assert r.status_code == 400


def test_missing_directory_rejected(client, tmp_path, audit_events):
    r = client.post("/api/desktop/folders/grant",
                    json={"root": str(tmp_path / "does-not-exist"), "mode": "read"})
    assert r.status_code == 400


def test_sensitive_root_refused(client, tmp_path, audit_events):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    r = client.post("/api/desktop/folders/grant", json={"root": str(ssh), "mode": "read"})
    assert r.status_code == 400
    assert "sensitive" in r.json()["detail"].lower()


def test_revoke_missing_is_false(client, audit_events):
    d = client.delete("/api/desktop/folders/grant/grt_nope")
    assert d.status_code == 200 and d.json()["revoked"] is False
