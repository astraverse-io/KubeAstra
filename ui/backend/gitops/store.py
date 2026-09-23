"""Short-lived preview store. Single-pod, same caveat as plans.PlanStore."""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

PREVIEW_TTL_SECONDS = 900  # 15 min, matching plan TTL


@dataclass
class Preview:
    token: str
    proposal_id: str
    repo_id: str
    files: dict[str, str]
    diff: str
    branch: str
    title: str
    body: str
    commit_msg: str
    labels: list[str]
    owner: str
    name: str
    base: str
    created_at: float
    expires_at: float


def new_preview(*, proposal_id, repo_id, files, diff, branch, title, body,
                commit_msg, labels, owner, name, base) -> Preview:
    now = time.time()
    return Preview(
        token="gpv_" + secrets.token_urlsafe(12),
        proposal_id=proposal_id, repo_id=repo_id, files=files, diff=diff,
        branch=branch, title=title, body=body, commit_msg=commit_msg,
        labels=labels, owner=owner, name=name, base=base,
        created_at=now, expires_at=now + PREVIEW_TTL_SECONDS,
    )


class PreviewStore:
    def __init__(self):
        self._items: dict[str, Preview] = {}
        self._lock = threading.Lock()

    def _purge_locked(self):
        now = time.time()
        for t in [t for t, p in self._items.items() if p.expires_at < now]:
            self._items.pop(t, None)

    def put(self, preview: Preview) -> None:
        with self._lock:
            self._purge_locked()
            self._items[preview.token] = preview

    def get(self, token: str) -> Preview | None:
        with self._lock:
            self._purge_locked()
            return self._items.get(token)

    def pop(self, token: str) -> Preview | None:
        with self._lock:
            self._purge_locked()
            return self._items.pop(token, None)


preview_store = PreviewStore()
