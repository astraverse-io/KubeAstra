from __future__ import annotations
import sys
from pathlib import Path
from unittest import mock

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import db  # noqa: E402
from gitops.index import RepoFile  # noqa: E402


def _reset_settings_cache():
    from config.settings import get_settings
    try:
        get_settings.cache_clear()
    except Exception:
        pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GITOPS_ENABLED", "true")
    _reset_settings_cache()
    from main import app
    db.init_db()
    with db._conn() as con:
        # children before parents — gitops_prs has an FK to gitops_repos
        con.execute("DELETE FROM gitops_prs")
        con.execute("DELETE FROM gitops_repos")
    yield TestClient(app)
    _reset_settings_cache()


def test_disabled_feature_returns_404(monkeypatch):
    monkeypatch.setenv("GITOPS_ENABLED", "false")
    _reset_settings_cache()
    from main import app
    c = TestClient(app)
    assert c.get("/api/gitops/repos").status_code == 404
    _reset_settings_cache()


def test_preview_then_open_opens_one_pr(client):
    client.post("/api/gitops/repos", json={"provider": "github",
                "owner": "astraverse-io", "name": "kubeastra-demo"})
    api_yaml = ("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n"
                "  name: api-gateway\nspec:\n  replicas: 3\n")

    fake_pr = mock.Mock(number=7,
                        url="https://github.com/astraverse-io/kubeastra-demo/pull/7",
                        branch="kubeastra/x")
    with mock.patch("routers.gitops._fetch_repo_files",
                    return_value=[RepoFile("base/api.yaml", api_yaml)]), \
         mock.patch("routers.gitops._open_pr_on_github", return_value=fake_pr), \
         mock.patch("routers.gitops.resolve_token", return_value="tok"):
        prev = client.post("/api/gitops/preview", json={
            "proposal_id": "p1",
            "investigation_id": "inv1",
            "diagnosis": {"title": "low replicas", "evidence": ["p99 high"]},
            "change": {"kind": "Deployment", "name": "api-gateway", "namespace": None,
                       "field_path": ["spec", "replicas"], "new_value": 5, "reason": "load"},
        })
        assert prev.status_code == 200, prev.text
        body = prev.json()
        assert "replicas: 5" in "".join(body["files"].values())
        assert body["diff"].count("\n") >= 1

        opened = client.post("/api/gitops/open",
                             json={"preview_token": body["preview_token"]})
        assert opened.status_code == 200, opened.text
        assert opened.json()["pr_number"] == 7

    assert len(db.list_gitops_prs()) == 1


def test_rollout_restart_is_refused(client):
    client.post("/api/gitops/repos", json={"provider": "github", "owner": "o", "name": "n"})
    with mock.patch("routers.gitops.resolve_token", return_value="tok"):
        r = client.post("/api/gitops/preview", json={
            "proposal_id": "p1", "investigation_id": "inv1",
            "diagnosis": {"title": "x", "evidence": []},
            "change": {"kind": "Deployment", "name": "api", "namespace": None,
                       "field_path": ["rollout_restart"], "new_value": "", "reason": "r"},
        })
        assert r.status_code == 422
