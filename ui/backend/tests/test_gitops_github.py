from __future__ import annotations
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import httpx  # noqa: E402
from gitops.github import GitHubClient, OpenedPR  # noqa: E402


def _transport(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        p = request.url.path
        if p.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "basesha"}})
        if p.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": "blobsha"})
        if p.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "treesha"})
        if p.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "commitsha"})
        if p.endswith("/git/refs"):
            return httpx.Response(201, json={"ref": "refs/heads/kubeastra/x"})
        if p.endswith("/pulls"):
            return httpx.Response(201, json={"number": 7,
                                             "html_url": "https://github.com/o/r/pull/7"})
        if "/labels" in p:
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "unexpected"})
    return httpx.MockTransport(handler)


def test_open_pr_issues_git_data_sequence_and_returns_pr():
    calls = []
    gh = GitHubClient("tok", transport=_transport(calls))
    pr = gh.open_pr(owner="o", name="r", base="main", head="kubeastra/x",
                    title="fix", body="b", files={"base/api.yaml": "..."},
                    commit_msg="fix", labels=["kubeastra"])
    assert isinstance(pr, OpenedPR) and pr.number == 7
    paths = [p for _, p in calls]
    # blob -> tree -> commit -> ref -> pull, in that order
    order = [p.rsplit("/", 1)[-1] for p in paths
             if any(s in p for s in ("blobs", "trees", "commits", "refs", "pulls"))]
    assert order == ["blobs", "trees", "commits", "refs", "pulls"]


def test_client_has_no_merge_method():
    # A merge method must never exist on this class.
    assert not hasattr(GitHubClient, "merge")
    assert not any("merge" in name for name in dir(GitHubClient))
