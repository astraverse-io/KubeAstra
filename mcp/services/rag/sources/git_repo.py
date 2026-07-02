"""Git-repo source: shallow-clone, then delegate to LocalPathSource.

Auth is intentionally kept simple: an optional token read from an env var
is injected into the clone URL when present. For SSH-cloned private repos
the host's SSH agent is used as-is.

URL rewriting (important — see plan §11.1)
------------------------------------------
``LocalPathSource`` sets ``Document.url`` to ``file://<tempdir>/...`` which
points at the clone tempdir of *this* job pod. That URL would (a) not
resolve as a citation at chat time, and (b) flip on every reindex run —
breaking ``ingestion._hash_chunk``'s idempotency key. So this wrapper
rewrites every yielded ``Document.url`` to a stable form:

  - GitHub repos      → ``https://github.com/<owner>/<repo>/blob/<branch>/<path>``
  - Other hosts        → ``git_repo:<original_url>@<branch>:<path>`` (stable
                         but not clickable; better than a tempdir path)

The repo URL, branch, and path-within-repo are also dropped into
``metadata`` so downstream consumers can reconstruct a different URL
shape later if we ever need to.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from typing import Iterator

from .base import Document
from .local_path import LocalPathSource

logger = logging.getLogger(__name__)


class GitRepoSource:
    name = "git_repo"

    def __init__(
        self,
        url: str,
        *,
        branch: str = "main",
        subdir: str = "",
        token_env: str | None = None,
        emit_role_aggregates: bool = False,
    ):
        """``emit_role_aggregates``: when true, after yielding all per-file
        Documents, also yield one synthetic ``Document(file_type=
        "role_aggregate")`` per role under ``<walk_root>/roles/*/*/``.
        Runs inside the same clone so the tempdir is still available."""
        if not _looks_safe(url):
            raise ValueError(f"git_repo: refusing unsafe URL {url!r}")
        self.url = url
        self.branch = branch
        self.subdir = subdir.strip("/")
        self.token_env = token_env
        self.emit_role_aggregates = emit_role_aggregates

    def discover(self) -> Iterator[Document]:
        clone_url = self._auth_url()
        tmpdir = tempfile.mkdtemp(prefix="rag-git-")
        try:
            cmd = ["git", "clone", "--depth", "1", "--branch", self.branch, clone_url, tmpdir]
            try:
                subprocess.run(
                    cmd, check=True, capture_output=True, text=True, timeout=120,
                )
            except subprocess.CalledProcessError as exc:
                # Scrub the token before logging: git frequently echoes the
                # full clone URL on failure ("fatal: could not read from
                # https://oauth2:ghp_xxx@github.com/..."), which would leak
                # the PAT into log aggregators.
                stderr = _scrub_token((exc.stderr or "").strip()[:500], self.token_env)
                logger.error("git_repo clone failed: %s (stderr=%s)", exc, stderr)
                return
            except subprocess.TimeoutExpired:
                logger.error("git_repo clone timed out for %s", self.url)
                return

            walk_root = tmpdir if not self.subdir else os.path.join(tmpdir, self.subdir)
            tagged_source = f"git_repo:{self.url}@{self.branch}"
            blob_base = self._blob_base()

            for doc in LocalPathSource(walk_root).discover():
                # LocalPathSource.title is path relative to walk_root. The
                # path *within the repo* needs ``subdir`` prepended.
                path_in_repo = (
                    f"{self.subdir}/{doc.title}" if self.subdir else doc.title
                )
                doc.metadata["source"] = tagged_source
                doc.metadata["repo_url"] = self.url
                doc.metadata["branch"] = self.branch
                doc.metadata["path"] = path_in_repo

                if blob_base:
                    doc.url = f"{blob_base}/{path_in_repo}"
                else:
                    # Non-GitHub host: synthesize a stable URL so the chunk
                    # hash is deterministic. Citation won't be clickable
                    # without UI support, but the index stays consistent.
                    doc.url = f"git_repo:{self.url}@{self.branch}:{path_in_repo}"

                yield doc

            # Second pass: yield per-role aggregate documents while the
            # clone is still on disk. Implemented as a lazy import so the
            # base file walk doesn't pull in the role-aggregate module
            # unnecessarily.
            if self.emit_role_aggregates:
                from services.rag.ansible_roles import RoleAggregateSource
                agg = RoleAggregateSource(
                    walk_root,
                    repo_url=self.url,
                    branch=self.branch,
                    path_prefix=self.subdir,
                )
                yield from agg.discover()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _auth_url(self) -> str:
        """Inject a token into the URL if token_env is set and non-empty.

        Supports https:// only. SSH URLs are returned as-is so the host's
        SSH agent handles auth.
        """
        if not self.token_env:
            return self.url
        token = os.environ.get(self.token_env, "")
        if not token:
            return self.url
        if not self.url.startswith("https://"):
            logger.debug("git_repo: token_env ignored for non-https URL")
            return self.url
        # https://user:token@host/path — `oauth2` is GitHub/GitLab convention.
        return self.url.replace("https://", f"https://oauth2:{token}@", 1)

    def _blob_base(self) -> str | None:
        """Compute the URL prefix that maps repo-relative paths to clickable
        web URLs. Returns ``None`` for hosts we don't recognize."""
        parsed = _parse_github_owner_repo(self.url)
        if parsed:
            owner, repo = parsed
            return f"https://github.com/{owner}/{repo}/blob/{self.branch}"
        return None


_SAFE_URL_RE = re.compile(r"^(?:https://|git@|ssh://)[A-Za-z0-9._\-:/@~]+$")

# HTTPS:  https://github.com/<owner>/<repo>[.git][/]
_GITHUB_HTTPS_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/.]+?)(?:\.git)?/?$"
)
# SSH:    git@github.com:<owner>/<repo>[.git]
_GITHUB_SSH_RE = re.compile(
    r"^git@github\.com:([^/]+)/([^/.]+?)(?:\.git)?$"
)


def _looks_safe(url: str) -> bool:
    """Reject URLs containing shell metacharacters. ``git clone`` accepts
    plain URLs without shell expansion, but we still belt-and-braces it."""
    return bool(_SAFE_URL_RE.match(url))


def _scrub_token(text: str, token_env: str | None) -> str:
    """Redact the auth token (if any) from a string before logging.

    Git error messages frequently include the URL that was used for the
    clone — which after ``_auth_url()`` injection contains the PAT. This
    helper replaces the literal token value with ``<redacted>`` so logs
    are safe to ship to aggregators.
    """
    if not token_env:
        return text
    token = os.environ.get(token_env, "")
    if not token:
        return text
    return text.replace(token, "<redacted>")


def _parse_github_owner_repo(url: str) -> tuple[str, str] | None:
    """Extract ``(owner, repo)`` for github.com URLs. Returns ``None`` for
    any non-github.com host (self-hosted GitHub Enterprise, Gitea, etc.).
    Adding GHE support later is a one-line config addition."""
    m = _GITHUB_HTTPS_RE.match(url)
    if m:
        return m.group(1), m.group(2)
    m = _GITHUB_SSH_RE.match(url)
    if m:
        return m.group(1), m.group(2)
    return None
