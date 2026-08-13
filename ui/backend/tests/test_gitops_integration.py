from __future__ import annotations
import os, sys, uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest  # noqa: E402

TOKEN = os.environ.get("GITOPS_TEST_TOKEN")

pytestmark = pytest.mark.skipif(not TOKEN, reason="GITOPS_TEST_TOKEN not set")

_OWNER = "astraverse-io"
_REPO = "kubeastra-demo"


def test_opens_and_closes_a_real_pr():
    from gitops.github import GitHubClient
    import httpx

    gh = GitHubClient(TOKEN)
    branch = f"kubeastra/it-{uuid.uuid4().hex[:8]}"
    pr = gh.open_pr(owner=_OWNER, name=_REPO, base="main", head=branch,
                    title="[it] replicas 2->3", body="integration test",
                    commit_msg="it", files={"base/api-gateway.yaml": _bump_replicas()},
                    labels=["kubeastra"])
    assert pr.number > 0
    # clean up: close the PR and delete the branch so the demo repo stays tidy
    c = httpx.Client(base_url="https://api.github.com",
                     headers={"Authorization": f"Bearer {TOKEN}", "User-Agent": "kubeastra"},
                     timeout=30.0)
    c.patch(f"/repos/{_OWNER}/{_REPO}/pulls/{pr.number}", json={"state": "closed"})
    c.request("DELETE", f"/repos/{_OWNER}/{_REPO}/git/refs/heads/{branch}")


def _bump_replicas() -> str:
    """Fetch the live base file and bump replicas 2 -> 3 via the real
    locator/editor, so the test exercises the same code path the router does."""
    import httpx
    from gitops.locate import find_span
    from gitops.edit import apply_span

    raw = httpx.get(
        f"https://raw.githubusercontent.com/{_OWNER}/{_REPO}/main/base/api-gateway.yaml",
        timeout=30.0,
    ).text
    span = find_span(raw, 0, ("spec", "replicas"))
    return apply_span(raw, span, 3)
