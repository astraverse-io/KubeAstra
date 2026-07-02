"""Per-role aggregate emitter (plan §11.3, option 1).

A single Ansible task chunked in isolation produces a vector for ~30
tokens of text, which doesn't carry enough context for the retriever to
match a user-pasted error to the right role. The fix is a *coarse* tier
sitting next to the fine-grained per-task chunks: one synthetic Document
per role containing the README, task names, and key defaults.

This module runs as a post-processing pass after per-file ingestion: it
walks ``<repo_root>/roles/*/*/`` directories, builds one
``Document(file_type="role_aggregate")`` per role, and yields it. The
chunker dispatcher in ``chunking.py`` maps that file_type to
``chunk_role_aggregate`` which emits exactly one chunk per role.

The same ``ingest()`` pipeline handles these — no separate Qdrant code
path. The reindex script invokes this after the file walk:

    ingest(LocalPathSource(repo_root) or GitRepoSource(...))   # files
    ingest(RoleAggregateSource(repo_root))                      # aggregates
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, Optional

import yaml

from services.rag.sources.base import Document
from services.rag.sources.git_repo import _parse_github_owner_repo

logger = logging.getLogger(__name__)


# Cap to keep aggregates under the per-chunk token budget even for very
# verbose roles. Aggregates that hit this limit get truncated with a
# marker — better than splitting which defeats the "single dense vector
# per role" goal.
_MAX_AGGREGATE_CHARS = 6000


class RoleAggregateSource:
    """Source-like iterator that yields one synthetic Document per role
    found under ``<root>/roles/*/*/``.

    Reads the per-role README, ``tasks/main.yaml`` (for task name list),
    and ``defaults/main.yaml`` (for variable keys). Builds a compact
    summary text and tags metadata so ``chunk_role_aggregate`` can route
    it correctly.
    """

    name = "role_aggregate"

    def __init__(
        self,
        path: str | Path,
        *,
        repo_url: Optional[str] = None,
        branch: Optional[str] = None,
        path_prefix: Optional[str] = None,
    ):
        """``path`` is the directory containing ``roles/``. For the
        deployment repo with ``subdir="ansible"`` that's the
        ``ansible/`` directory. Named ``path`` (rather than
        ``repo_root``) for consistency with ``LocalPathSource`` so
        reindex.py YAML configs can use the same key.

        ``repo_url``/``branch``/``path_prefix`` are passed through so the
        synthetic Documents' ``url`` matches what ``GitRepoSource``
        produces for the per-file documents (GitHub blob URL for the
        role's directory). Without this, citations on aggregates would
        point at file:// paths and break.
        """
        self.repo_root = Path(path).expanduser().resolve()
        self.repo_url = repo_url
        self.branch = branch
        self.path_prefix = (path_prefix or "").strip("/")

    def discover(self) -> Iterator[Document]:
        roles_dir = self.repo_root / "roles"
        if not roles_dir.is_dir():
            logger.info("role_aggregate: no roles/ dir at %s", roles_dir)
            return

        for category_dir in sorted(roles_dir.iterdir()):
            if not category_dir.is_dir():
                continue
            category = category_dir.name
            for role_dir in sorted(category_dir.iterdir()):
                if not role_dir.is_dir():
                    continue
                role = role_dir.name
                try:
                    doc = self._build_doc(category, role, role_dir)
                except Exception as exc:
                    # One bad role doesn't kill the run; the per-file
                    # ingestion already covered the underlying files.
                    logger.warning(
                        "role_aggregate: skipped %s/%s (%s)",
                        category, role, exc,
                    )
                    continue
                if doc is not None:
                    yield doc

    def _build_doc(
        self, category: str, role: str, role_dir: Path,
    ) -> Optional[Document]:
        readme = _read_first_existing(
            role_dir / "README.md",
            role_dir / "Readme.md",
            role_dir / "readme.md",
        )
        task_names = _extract_task_names(role_dir / "tasks" / "main.yaml")
        if not task_names:
            task_names = _extract_task_names(role_dir / "tasks" / "main.yml")
        default_keys = _extract_default_keys(role_dir / "defaults" / "main.yaml")
        if not default_keys:
            default_keys = _extract_default_keys(role_dir / "defaults" / "main.yml")

        # If a role has neither README nor tasks nor defaults, there's
        # nothing meaningful to aggregate — skip rather than embed an
        # empty vector.
        if not readme and not task_names and not default_keys:
            return None

        body = _render_aggregate(
            category=category,
            role=role,
            readme=readme,
            task_names=task_names,
            default_keys=default_keys,
        )
        if len(body) > _MAX_AGGREGATE_CHARS:
            body = body[:_MAX_AGGREGATE_CHARS] + "\n[...truncated]\n"

        path_in_repo = self._path_in_repo(role_dir)
        url = self._url_for(path_in_repo)
        return Document(
            url=url,
            title=f"role_aggregate:{category}/{role}",
            content=body,
            metadata={
                "source": f"role_aggregate:{self.repo_root}",
                "file_type": "role_aggregate",
                "category": category,
                "role": role,
                "path": path_in_repo,
                "repo_url": self.repo_url or "",
                "branch": self.branch or "",
            },
        )

    def _path_in_repo(self, role_dir: Path) -> str:
        rel = role_dir.relative_to(self.repo_root).as_posix()
        if self.path_prefix:
            return f"{self.path_prefix}/{rel}"
        return rel

    def _url_for(self, path_in_repo: str) -> str:
        # Aggregates point at the role *directory*, so we build a
        # ``tree/`` URL rather than the file-flavored ``blob/`` URL that
        # GitRepoSource uses. Both reuse the same owner/repo parser to
        # avoid the two having different ideas of what a valid GitHub URL
        # looks like.
        if self.repo_url and self.branch:
            parsed = _parse_github_owner_repo(self.repo_url)
            if parsed:
                owner, repo = parsed
                return (
                    f"https://github.com/{owner}/{repo}/tree/{self.branch}/"
                    f"{path_in_repo}"
                )
        # Fallback to a stable synthetic URL — same shape as GitRepoSource
        # uses for non-GitHub hosts, so the chunk-hash stays deterministic.
        return f"role_aggregate:{self.repo_url or self.repo_root}@{self.branch or 'local'}:{path_in_repo}"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _read_first_existing(*paths: Path) -> str:
    for p in paths:
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
    return ""


def _extract_task_names(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    names: list[str] = []
    for task in data:
        if not isinstance(task, dict):
            continue
        n = task.get("name")
        if isinstance(n, str) and n.strip():
            names.append(n.strip())
        # Also flatten block/rescue task names — they describe behavior
        # the role provides and improve aggregate recall.
        for wrap in ("block", "rescue", "always"):
            inner = task.get(wrap)
            if isinstance(inner, list):
                for t in inner:
                    if isinstance(t, dict):
                        nn = t.get("name")
                        if isinstance(nn, str) and nn.strip():
                            names.append(nn.strip())
    return names


def _extract_default_keys(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    return [str(k) for k in data.keys()]


def _render_aggregate(
    *,
    category: str,
    role: str,
    readme: str,
    task_names: list[str],
    default_keys: list[str],
) -> str:
    out: list[str] = [f"# Role: {category}/{role}"]
    if readme.strip():
        out.append("\n## README\n")
        out.append(readme.strip())
    if task_names:
        out.append("\n## Tasks\n")
        for n in task_names:
            out.append(f"- {n}")
    if default_keys:
        out.append("\n## Default variables\n")
        for k in default_keys:
            out.append(f"- {k}")
    return "\n".join(out) + "\n"


