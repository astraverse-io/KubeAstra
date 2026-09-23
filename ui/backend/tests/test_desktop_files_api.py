"""Endpoint tests for /api/desktop/files/* (Phase 2, PR 4).

Apply is the only code path that writes a user's file, so it re-runs the whole
boundary at apply time instead of trusting what was checked at propose time:
the write grant must still be held, the target re-resolved (no symlink, not
deny-listed, same path), the file unchanged since the preview, then an atomic
write. Every refusal is audited as folder.write_denied; a success as
folder.write — never with file content. Tokens are single-use even on failure.
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

import desktop_folders as df  # noqa: E402
import desktop_validators as dv  # noqa: E402
import desktop_writes as dw  # noqa: E402
from routers import desktop_files as files_router  # noqa: E402

API = ("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\n"
       "  annotations:\n    deploy-token: ghp_0123456789abcdef0123456789abcdef0123\n"
       "spec:\n  replicas: 2\n")


class Ctx:
    session_id = "s1"


@pytest.fixture
def events(tmp_path, monkeypatch):
    import audit
    import desktop_paths
    monkeypatch.setattr(desktop_paths, "config_path", lambda: tmp_path / "cfg.json")
    monkeypatch.setattr(dv, "_found", lambda tool: None)
    monkeypatch.setattr(dw, "pending_write_store", dw.PendingWriteStore())
    monkeypatch.setattr(dw, "_session_cluster", lambda session_id: None)   # no cluster connected
    dw._invalid_attempts.clear()
    captured = []
    monkeypatch.setattr(audit, "emit", lambda et, **kw: captured.append((et, kw)) or "evt")
    return captured


@pytest.fixture
def client(events):
    app = FastAPI()
    app.include_router(files_router.router, prefix="/api")
    return TestClient(app)


@pytest.fixture
def repo(tmp_path, events):
    root = (tmp_path / "infra").resolve()
    root.mkdir()
    (root / "api.yaml").write_text(API)
    df.add_grant(str(root), "write")
    return root


def _propose(path, **kw):
    params = {"path": str(path), "reason": "scale", "edits": kw.get("edits", [])}
    if "content" in kw:
        params["content"] = kw["content"]
    out = dw._handle_propose_file_edit(params, Ctx())
    assert "pending_write" in out, out
    return out["pending_write"]["token"]


REPLICAS = [{"old": "replicas: 2", "new": "replicas: 4"}]


def _types(events):
    import audit
    return [et for et, _ in events if et in (audit.EventType.FOLDER_WRITE, audit.EventType.FOLDER_WRITE_DENIED)]


class TestPending:
    def test_get_returns_the_real_diff_for_approval(self, client, repo):
        token = _propose(repo / "api.yaml", edits=[{"old": "  name: api\n",
                                                    "new": "  name: api\n  labels: {tier: web}\n"}])
        r = client.get(f"/api/desktop/files/pending/{token}")
        assert r.status_code == 200
        body = r.json()
        assert body["path"] == "api.yaml" and body["created"] is False
        # The human approves the exact bytes that will be written, context included.
        assert "ghp_0123456789abcdef0123456789abcdef0123" in body["diff"]
        assert "+  labels: {tier: web}" in body["diff"]
        assert body["validation"]["ok"] is True
        assert "after" not in body and "before" not in body

    def test_get_unknown_is_404(self, client, repo):
        assert client.get("/api/desktop/files/pending/pwr_nope").status_code == 404

    def test_get_does_not_consume(self, client, repo):
        token = _propose(repo / "api.yaml", edits=REPLICAS)
        client.get(f"/api/desktop/files/pending/{token}")
        assert client.get(f"/api/desktop/files/pending/{token}").status_code == 200


class TestApply:
    def test_happy_path_writes_atomically_and_audits(self, client, repo, events):
        import audit
        token = _propose(repo / "api.yaml", edits=REPLICAS)
        r = client.post("/api/desktop/files/apply", json={"token": token})
        assert r.status_code == 200 and r.json() == {"written": True, "path": "api.yaml", "created": False}
        text = (repo / "api.yaml").read_text()
        assert "replicas: 4" in text and "ghp_0123456789abcdef0123456789abcdef0123" in text
        writes = [kw for et, kw in events if et == audit.EventType.FOLDER_WRITE]
        assert len(writes) == 1
        p = writes[0]["payload"]
        assert p["rel_path"] == "api.yaml" and p["root"] == str(repo)
        assert p["bytes_after"] > 0 and "validated" in p
        assert "replicas" not in str(p) and "ghp_" not in str(p)    # never content

    def test_single_use(self, client, repo):
        token = _propose(repo / "api.yaml", edits=REPLICAS)
        assert client.post("/api/desktop/files/apply", json={"token": token}).status_code == 200
        assert client.post("/api/desktop/files/apply", json={"token": token}).status_code == 404

    def test_unknown_token_404(self, client, repo):
        assert client.post("/api/desktop/files/apply", json={"token": "pwr_x"}).status_code == 404

    def test_expired_404(self, client, repo, monkeypatch):
        token = _propose(repo / "api.yaml", edits=REPLICAS)
        pw = dw.pending_write_store.get(token)
        monkeypatch.setattr(dw.time, "time", lambda: pw.expires_at + 1)
        assert client.post("/api/desktop/files/apply", json={"token": token}).status_code == 404
        assert (repo / "api.yaml").read_text() == API

    def test_new_file_created(self, client, repo):
        token = _propose(repo / "extra.yaml", content="apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n")
        r = client.post("/api/desktop/files/apply", json={"token": token})
        assert r.status_code == 200 and r.json()["created"] is True
        assert (repo / "extra.yaml").exists()


class TestApplyRechecksTheBoundary:
    def test_grant_revoked_is_403(self, client, repo, events):
        import audit
        token = _propose(repo / "api.yaml", edits=REPLICAS)
        for g in df.list_grants():
            df.revoke_grant(g["id"])
        r = client.post("/api/desktop/files/apply", json={"token": token})
        assert r.status_code == 403
        assert (repo / "api.yaml").read_text() == API
        denied = [kw for et, kw in events if et == audit.EventType.FOLDER_WRITE_DENIED]
        assert denied and denied[0]["payload"]["reason"] == "no_write_grant"

    def test_grant_downgraded_to_read_is_403(self, client, repo):
        token = _propose(repo / "api.yaml", edits=REPLICAS)
        for g in df.list_grants():
            df.revoke_grant(g["id"])
        df.add_grant(str(repo), "read")
        assert client.post("/api/desktop/files/apply", json={"token": token}).status_code == 403
        assert (repo / "api.yaml").read_text() == API

    def test_file_changed_since_preview_is_409(self, client, repo, events):
        token = _propose(repo / "api.yaml", edits=REPLICAS)
        (repo / "api.yaml").write_text(API + "# edited by the user\n")
        r = client.post("/api/desktop/files/apply", json={"token": token})
        assert r.status_code == 409 and "changed" in r.json()["detail"]
        assert (repo / "api.yaml").read_text().endswith("# edited by the user\n")
        import audit
        assert _types(events)[-1] == audit.EventType.FOLDER_WRITE_DENIED

    def test_new_file_that_now_exists_is_409(self, client, repo):
        token = _propose(repo / "extra.yaml",
                         content="apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n")
        (repo / "extra.yaml").write_text("someone: else\n")
        assert client.post("/api/desktop/files/apply", json={"token": token}).status_code == 409
        assert (repo / "extra.yaml").read_text() == "someone: else\n"

    def test_edited_file_deleted_is_409(self, client, repo):
        token = _propose(repo / "api.yaml", edits=REPLICAS)
        (repo / "api.yaml").unlink()
        assert client.post("/api/desktop/files/apply", json={"token": token}).status_code == 409
        assert not (repo / "api.yaml").exists()

    def test_target_swapped_for_symlink_is_refused(self, client, repo, tmp_path):
        token = _propose(repo / "api.yaml", edits=REPLICAS)
        victim = tmp_path / "victim.yaml"
        victim.write_text(API)
        (repo / "api.yaml").unlink()
        (repo / "api.yaml").symlink_to(victim)
        r = client.post("/api/desktop/files/apply", json={"token": token})
        assert r.status_code == 400 and "symlink" in r.json()["detail"]
        assert victim.read_text() == API

    def test_failed_apply_still_consumes_token(self, client, repo):
        token = _propose(repo / "api.yaml", edits=REPLICAS)
        (repo / "api.yaml").write_text(API + "# changed\n")
        assert client.post("/api/desktop/files/apply", json={"token": token}).status_code == 409
        (repo / "api.yaml").write_text(API)
        assert client.post("/api/desktop/files/apply", json={"token": token}).status_code == 404


class TestDiscard:
    def test_discard(self, client, repo):
        token = _propose(repo / "api.yaml", edits=REPLICAS)
        assert client.post("/api/desktop/files/discard", json={"token": token}).json() == {"discarded": True}
        assert client.post("/api/desktop/files/apply", json={"token": token}).status_code == 404
        assert client.post("/api/desktop/files/discard", json={"token": token}).json() == {"discarded": False}
        assert (repo / "api.yaml").read_text() == API
