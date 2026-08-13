from __future__ import annotations
import re, sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

GITOPS = BACKEND_DIR / "gitops"


def test_no_merge_endpoint_anywhere_in_gitops():
    offenders = []
    for py in GITOPS.glob("*.py"):
        text = py.read_text()
        # a PUT to /pulls/{n}/merge, or a .merge( call — either is forbidden
        if re.search(r"/pulls/[^\"']*merge", text) or re.search(r"\.merge\s*\(", text):
            offenders.append(py.name)
    assert offenders == [], f"merge path present in: {offenders}"


def test_github_client_exposes_no_merge():
    from gitops.github import GitHubClient
    assert not any("merge" in n for n in dir(GitHubClient))
