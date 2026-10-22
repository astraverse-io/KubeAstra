"""Resilience Pilot Phase 2: a field fix round-trips through the shipped GitOps
PR path (POST /api/gitops/preview) — no new write path.

The resilience core produces ``Fix.gitops`` in the ``ChangeBody`` shape; these
tests post it unchanged to the real router with only the GitHub tarball fetch
patched, and check the diff is exactly the intended one-value edit.
"""
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
from services.resilience import assess, scan_path  # noqa: E402

FIXTURE = MCP_DIR / "tests" / "fixtures" / "resilience" / "payments" / "payments.yaml"


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
        con.execute("DELETE FROM gitops_prs")
        con.execute("DELETE FROM gitops_repos")
    c = TestClient(app)
    c.post("/api/gitops/repos", json={"provider": "github", "owner": "acme", "name": "deploy"})
    yield c
    _reset_settings_cache()


def _preview(client, files, change):
    with mock.patch("routers.gitops._fetch_repo_files", return_value=files), \
         mock.patch("routers.gitops.resolve_token", return_value="tok"):
        return client.post("/api/gitops/preview", json={
            "proposal_id": "res-1", "investigation_id": "",
            "diagnosis": {"title": "resilience", "evidence": []},
            "change": change,
        })


def _fix(rule, path):
    return next(f.fix for f in assess(scan_path(path)).findings if f.rule == rule)


def test_res001_fix_previews_as_a_one_line_pr(client):
    fix = _fix("RES001", FIXTURE)
    assert fix.kind == "field" and fix.gitops is not None
    r = _preview(client, [RepoFile("apps/payments.yaml", FIXTURE.read_text())], fix.gitops)
    assert r.status_code == 200, r.text
    diff = r.json()["diff"]
    assert "-  minAvailable: 3" in diff and "+  minAvailable: 2" in diff
    changed = [l for l in diff.splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    assert len(changed) == 2, diff  # exactly one line out, one line in — comments and order untouched


def test_percentage_stays_a_quoted_percentage(client, tmp_path):
    text = FIXTURE.read_text().replace("minAvailable: 3", 'minAvailable: "100%"')
    src = tmp_path / "payments.yaml"
    src.write_text(text)
    fix = _fix("RES001", src)
    assert fix.new_value == "66%"
    r = _preview(client, [RepoFile("apps/payments.yaml", text)], fix.gitops)
    assert r.status_code == 200, r.text
    assert '+  minAvailable: "66%"' in r.json()["diff"]


def test_missing_field_is_not_offered_as_a_pr(tmp_path):
    """RES004 on an HPA with no minReplicas: a patch can add it, the span editor can't."""
    (tmp_path / "hpa.yaml").write_text(
        "apiVersion: autoscaling/v2\nkind: HorizontalPodAutoscaler\nmetadata: {name: web, namespace: shop}\n"
        "spec:\n  scaleTargetRef: {apiVersion: apps/v1, kind: Deployment, name: web}\n  maxReplicas: 5\n"
    )
    fix = _fix("RES004", tmp_path)
    assert fix.kind == "field" and fix.yaml and fix.gitops is None


def test_helm_rendered_objects_are_never_prd(tmp_path):
    text = FIXTURE.read_text().replace(
        "  name: payments-pdb\n", "  name: payments-pdb\n  labels: {helm.sh/chart: payments-1.2.0}\n")
    (tmp_path / "p.yaml").write_text(text)
    fix = _fix("RES001", tmp_path)
    assert fix.kind == "advisory" and fix.gitops is None
    assert "payments-1.2.0" in fix.note and "minAvailable: 2" in fix.yaml
