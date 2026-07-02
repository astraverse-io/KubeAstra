"""Filesystem-walking source: yields markdown, YAML, Jinja, and custom
Ansible-module Python files under a directory.

Per-file metadata set here so the chunker dispatcher can pick the right
strategy without re-deriving from the path:
  - file_type: one of {markdown, yaml, jinja, ansible_module}
  - path:      path relative to the source root (POSIX-style)
  - category / role / role_subdir / play_group / environment: filled when
    the file lives under a recognized Ansible directory layout.

Secrets safety:
  - any file whose first non-empty line starts with ``$ANSIBLE_VAULT`` is
    skipped (vault-encrypted)
  - downstream ``services/rag/redaction.py`` runs over chunk text before
    embedding so plaintext patterns (PEM blocks, AWS keys, etc.) are scrubbed

A filename-based heuristic was deliberately *not* added: real Ansible
repos legitimately have playbooks/tasks/templates with ``secret`` or
``credential`` in the name (e.g. K8s Secret manifest templates, AWX
playbooks that *create* credential resources) that we'd lose if we
filtered on filename.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

from .base import Document

logger = logging.getLogger(__name__)


# (glob, file_type) — order matters only for logging; files are deduped
# by path so a single file is yielded once even if it could match
# multiple patterns.
_GLOBS_BY_TYPE: tuple[tuple[str, str], ...] = (
    ("**/*.md", "markdown"),
    ("**/*.markdown", "markdown"),
    ("**/*.yaml", "yaml"),
    ("**/*.yml", "yaml"),
    ("**/*.j2", "jinja"),
    # Custom Ansible modules only — anything else in Python land is noise.
    ("**/library/**/*.py", "ansible_module"),
)

# Path segments that disqualify a file outright.
_SKIP_DIR_SEGMENTS: frozenset[str] = frozenset({
    ".git",
    "__pycache__",
    "molecule",        # role test fixtures, not source-of-truth
    "node_modules",
    ".tox",
    ".venv",
})


class LocalPathSource:
    name = "local_path"

    def __init__(self, path: str, *, recursive: bool = True):
        self.root = Path(path).expanduser().resolve()
        self.recursive = recursive

    def discover(self) -> Iterator[Document]:
        if not self.root.exists():
            logger.warning("local_path source: root does not exist: %s", self.root)
            return
        if not self.root.is_dir():
            logger.warning("local_path source: root is not a directory: %s", self.root)
            return

        seen: set[Path] = set()
        for pattern, file_type in _GLOBS_BY_TYPE:
            iterator = (
                self.root.rglob(pattern[3:]) if self.recursive
                else self.root.glob(pattern)
            )
            for p in iterator:
                if p in seen:
                    continue
                if not p.is_file():
                    continue
                if _should_skip(self.root, p):
                    continue

                try:
                    content = p.read_text(encoding="utf-8", errors="replace")
                except Exception as exc:
                    logger.warning("local_path: skipping unreadable %s: %s", p, exc)
                    continue
                if not content.strip():
                    continue
                if _is_vault_encrypted(content):
                    # debug, not info: repos with hundreds of vault files
                    # would otherwise produce hundreds of log lines per
                    # reindex run.
                    logger.debug("local_path: skipping vault-encrypted file %s", p)
                    continue

                seen.add(p)
                rel = p.relative_to(self.root)
                metadata: dict = {
                    "source": f"local_path:{self.root}",
                    "file_type": file_type,
                    "path": rel.as_posix(),
                }
                metadata.update(_derive_ansible_metadata(rel))

                yield Document(
                    url=f"file://{p}",
                    title=rel.as_posix(),
                    content=content,
                    metadata=metadata,
                )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _should_skip(root: Path, p: Path) -> bool:
    """True if the path should not be ingested for any reason."""
    try:
        rel = p.relative_to(root)
    except ValueError:
        # Shouldn't happen with rglob, but be defensive.
        return True

    for seg in rel.parts:
        if seg in _SKIP_DIR_SEGMENTS:
            return True

    if p.suffix == ".retry":
        return True

    return False


def _is_vault_encrypted(content: str) -> bool:
    """Ansible Vault files begin with ``$ANSIBLE_VAULT;<version>;<cipher>``
    on the first non-empty line. We don't try to decrypt — just refuse."""
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        return line.startswith("$ANSIBLE_VAULT")
    return False


def _derive_ansible_metadata(rel: Path) -> dict[str, str]:
    """Pull out role/category/playbook/inventory hints from the path.

    Assumes the source root is the ``ansible/`` directory of a standard
    Ansible-roles repo. Quietly returns ``{}`` if the layout doesn't match —
    files outside the recognized structure (top-level READMEs, etc.) still
    get ingested, just without the extra metadata.
    """
    parts = rel.parts
    meta: dict[str, str] = {}

    if not parts:
        return meta

    head = parts[0]

    # roles/<category>/<role>/...
    # - At depth 4 (roles/cat/role/<file>) the 4th part is a file, not a
    #   subdir (e.g. roles/.../README.md). Tag role+category but skip
    #   role_subdir.
    # - At depth >= 5 the 4th part is the canonical Ansible subdir
    #   (tasks/defaults/handlers/templates/vars/meta) — or ``library`` for
    #   role-scoped custom modules.
    if head == "roles" and len(parts) >= 3:
        meta["category"] = parts[1]
        meta["role"] = parts[2]
        if len(parts) >= 5:
            meta["role_subdir"] = parts[3]
        # Role-local custom modules at roles/<cat>/<role>/library/<mod>.py
        # also deserve the ``module_name`` tag so citations show the
        # module name rather than just "library > " (same treatment as
        # repo-level library/ below).
        if "library" in parts and rel.suffix == ".py":
            meta["module_name"] = rel.stem
        return meta

    # playbooks/<group>/<file> — the second segment is the group only when
    # there's a file under it. A bare ``playbooks/README.md`` at depth 2
    # has no group.
    if head == "playbooks" and len(parts) >= 3:
        meta["play_group"] = parts[1]
        return meta

    # inventory/<env>/... — same depth-3 rule. ``inventory/OpenStack_README.md``
    # at depth 2 is a top-level doc, not an environment.
    if head == "inventory" and len(parts) >= 3:
        meta["environment"] = parts[1]
        # Track inventory sub-shape so chunker can decide whole-file vs.
        # per-entry treatment later.
        meta["inventory_kind"] = parts[2]  # e.g. group_vars / host_vars
        return meta

    # library/<...>.py — file_type already set to ansible_module; tag the
    # module name from the filename for citations.
    if "library" in parts and rel.suffix == ".py":
        meta["module_name"] = rel.stem

    return meta
