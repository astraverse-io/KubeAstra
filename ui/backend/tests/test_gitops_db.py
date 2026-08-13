from __future__ import annotations
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import db  # noqa: E402


def test_repo_crud_roundtrip():
    db.init_db()
    with db._conn() as con:
        con.execute("DELETE FROM gitops_repos")
    row = db.create_gitops_repo(repo_id="r1", provider="github", owner="o",
                                name="n", default_branch="main", config_path="kubeastra.yaml")
    assert row["owner"] == "o"
    assert any(r["id"] == "r1" for r in db.list_gitops_repos())
    db.delete_gitops_repo("r1")
    assert db.get_gitops_repo("r1") is None


def test_rate_limit_counts_recent_prs():
    db.init_db()
    with db._conn() as con:
        con.execute("DELETE FROM gitops_prs")
        con.execute("DELETE FROM gitops_repos")
    db.create_gitops_repo(repo_id="r1", provider="github", owner="o", name="n",
                          default_branch="main", config_path="kubeastra.yaml")
    for i in range(3):
        db.create_gitops_pr(pr_id=f"pr{i}", repo_id="r1", session_id="s",
                            investigation_id="inv", proposal_id="p", branch="kubeastra/x",
                            provider_pr_number=i, provider_pr_url="u", status="open",
                            files_changed=["a.yaml"], diff_summary="+1 -1")
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert db.count_recent_gitops_prs("r1", since) == 3
