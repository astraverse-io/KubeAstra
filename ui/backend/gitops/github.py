"""GitHub Git Data client over httpx. Opens PRs; never merges them."""
from __future__ import annotations

import base64
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class OpenedPR:
    number: int
    url: str
    branch: str


class GitHubClient:
    def __init__(self, token: str, *, api_base: str = "https://api.github.com",
                 transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            base_url=api_base,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "kubeastra",
            },
            transport=transport,
            timeout=30.0,
        )

    def _post(self, path: str, json: dict) -> dict:
        r = self._client.post(path, json=json)
        r.raise_for_status()
        return r.json()

    def can_read(self, owner: str, name: str) -> bool:
        r = self._client.get(f"/repos/{owner}/{name}")
        return r.status_code == 200

    def open_pr(self, *, owner: str, name: str, base: str, head: str,
                title: str, body: str, files: dict[str, str], commit_msg: str,
                labels: list[str]) -> OpenedPR:
        if head == base:
            raise ValueError("refusing to open a PR from the base branch onto itself")
        repo = f"/repos/{owner}/{name}"

        base_sha = self._client.get(f"{repo}/git/ref/heads/{base}").json()["object"]["sha"]

        tree_entries = []
        for path, content in files.items():
            blob = self._post(f"{repo}/git/blobs", {
                "content": base64.b64encode(content.encode()).decode(),
                "encoding": "base64",
            })
            tree_entries.append({"path": path, "mode": "100644",
                                 "type": "blob", "sha": blob["sha"]})

        tree = self._post(f"{repo}/git/trees",
                          {"base_tree": base_sha, "tree": tree_entries})
        commit = self._post(f"{repo}/git/commits",
                            {"message": commit_msg, "tree": tree["sha"],
                             "parents": [base_sha]})
        self._post(f"{repo}/git/refs",
                   {"ref": f"refs/heads/{head}", "sha": commit["sha"]})
        pr = self._post(f"{repo}/pulls",
                        {"title": title, "body": body, "head": head, "base": base})
        if labels:
            self._post(f"{repo}/issues/{pr['number']}/labels", {"labels": labels})
        return OpenedPR(number=pr["number"], url=pr["html_url"], branch=head)
