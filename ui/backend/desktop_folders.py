"""Desktop folder-access boundary — the security core (Phase 1, PR 1).

The desktop agent runs *as the user*: at the OS level it can already read
``~/.ssh`` and write ``~/.zshrc``. There is no OS sandbox, so **this module is the
only boundary**. It assumes the model may be compromised mid-run by prompt
injection (a hostile string in a pod log, a Kubernetes event, a ConfigMap, or a
file it just read) and tries to make an escape impossible even then.

Every access is gated by, in order:

1. **Grant** — a human picked the root in a native OS dialog; nothing outside a
   granted root is reachable, and the agent cannot self-grant.
2. **Containment** — ``Path.resolve()`` (resolves symlinks and ``..``) then
   ``is_relative_to(granted_root)``. Defeats traversal *and* a symlink inside the
   grant that points outside.
3. **Deny-list** — even inside a granted root, secret-ish files (``*.pem``,
   ``id_rsa``, ``.env*``, ``*secret*``, ``.ssh/``, ``.aws/``, ``.kube/`` …) are
   refused. Not overridable in v1.
4. **Caps** — per-file size cap; binary files refused.

Content redaction/sanitisation, the read primitives that emit ``folder.read``
audit events, the four desktop-only tools, and ``find_source_for_workload`` build
on this core in the following PRs. This module is kept dependency-light and
exhaustively unit-tested first (``tests/test_desktop_folders_boundary.py``);
nothing else in the feature lands until these tests are green.

See internal_docs/features/DESKTOP_AGENT_DESIGN.md §3.2 and
DESKTOP_AGENT_PHASE1_SPEC.md §3 (PR 1).
"""

from __future__ import annotations

import fnmatch
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── caps (design §3.2) ─────────────────────────────────────────────────────────
MAX_FILE_BYTES = 1_048_576          # 1 MB per file
_BINARY_SNIFF_BYTES = 8192          # a NUL byte in the first chunk ⇒ binary

# ── deny-list (design §3.2); NOT overridable in v1 ──────────────────────────────
# Filename globs, matched case-insensitively.
DENY_GLOBS = (
    "*.pem", "*.key", "id_rsa*", "id_ed25519*", "*.env", ".env*", "*secret*",
)
# Any path segment equal to one of these denies the whole path (e.g. ``.git/config``,
# ``.ssh/known_hosts``, ``.aws/credentials``, ``.kube/config``).
DENY_DIR_SEGMENTS = frozenset({".git", ".ssh", ".aws", ".kube"})

_GRANTS_KEY = "folder_grants"


# ── exceptions ──────────────────────────────────────────────────────────────────

class AccessDenied(Exception):
    """A path was inside the boundary's remit but refused (escape, deny-list, caps)."""

    def __init__(self, path: str, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"access denied ({reason}): {path}")


class NeedsAccess(Exception):
    """No grant covers this path yet — the caller must ask the human for one.

    A tool turns this into the ``{needs_access: {path, mode, reason}}`` contract
    that pauses the run and prompts the user (Phase 1, PR 6).
    """

    def __init__(self, path: str, mode: str, reason: str = ""):
        self.path = path
        self.mode = mode
        self.reason = reason
        super().__init__(f"needs {mode} access: {path}")


# ── containment: the single most important function in the feature ──────────────

_CASE_INSENSITIVE: Optional[bool] = None


def _fs_case_insensitive() -> bool:
    """Probe (once, cached) whether the filesystem is case-insensitive — macOS's
    default. Used so containment case-folds *only* where the FS itself does; on a
    case-sensitive FS we must NOT fold (that would let ``/a/Grant`` reach
    ``/a/grant``)."""
    global _CASE_INSENSITIVE
    if _CASE_INSENSITIVE is None:
        try:
            d = tempfile.mkdtemp(prefix="kubeastra_case_")
            (Path(d) / "caseprobe").write_text("x")
            _CASE_INSENSITIVE = (Path(d) / "CASEPROBE").exists()
            (Path(d) / "caseprobe").unlink()
            os.rmdir(d)
        except Exception:
            _CASE_INSENSITIVE = False
    return _CASE_INSENSITIVE


def _contained(resolved: Path, root: Path) -> bool:
    """Is ``resolved`` inside ``root``? Compared component-by-component (not by
    string prefix, so ``/a/grant-evil`` is NOT inside ``/a/grant``), and
    case-folded only on a case-insensitive FS (so a differently-cased path to a
    file genuinely inside the grant is not falsely rejected on macOS)."""
    rparts = resolved.parts
    root_parts = root.parts
    if len(rparts) < len(root_parts):
        return False
    if _fs_case_insensitive():
        return all(a.casefold() == b.casefold() for a, b in zip(root_parts, rparts))
    return rparts[: len(root_parts)] == root_parts


def contain_path(candidate, granted_root: Path) -> Path:
    """Resolve ``candidate`` (following symlinks and ``..``) and require the result
    to sit inside ``granted_root``. ``granted_root`` is assumed already resolved.

    Raises ``AccessDenied('outside_grant')`` on any escape. Returns the resolved,
    contained path.
    """
    resolved = Path(candidate).resolve()
    if not _contained(resolved, Path(granted_root)):
        raise AccessDenied(str(candidate), "outside_grant")
    return resolved


# ── deny-list ────────────────────────────────────────────────────────────────────

def is_denied(resolved: Path, granted_root: Path) -> bool:
    """True if a resolved, already-contained path is on the deny-list.

    Computed by slicing off the grant-root components (rather than
    ``relative_to``, which is case-sensitive and would raise on a legitimately
    case-folded path that ``contain_path`` already accepted)."""
    root_len = len(Path(granted_root).parts)
    rel_parts = resolved.parts[root_len:]
    for part in rel_parts:
        if part in DENY_DIR_SEGMENTS:
            return True
    name = resolved.name.lower()
    return any(fnmatch.fnmatch(name, pat) for pat in DENY_GLOBS)


# ── caps ──────────────────────────────────────────────────────────────────────────

def within_caps(resolved: Path) -> None:
    """Raise ``AccessDenied`` if the file is too large or binary. No return value."""
    try:
        size = resolved.stat().st_size
    except OSError:
        raise AccessDenied(str(resolved), "unreadable")
    if size > MAX_FILE_BYTES:
        raise AccessDenied(str(resolved), "too_large")
    try:
        with open(resolved, "rb") as fh:
            chunk = fh.read(_BINARY_SNIFF_BYTES)
    except OSError:
        raise AccessDenied(str(resolved), "unreadable")
    if b"\x00" in chunk:
        raise AccessDenied(str(resolved), "binary")


# ── grant registry (config, not credentials — persisted via desktop_config) ─────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_grants() -> list[dict]:
    import desktop_config
    return list(desktop_config.load().get(_GRANTS_KEY, []))


def _save_grants(grants: list[dict]) -> None:
    import desktop_config
    desktop_config.save({_GRANTS_KEY: grants})


def _mode_satisfies(grant_mode: str, requested: str) -> bool:
    """A ``write`` grant satisfies a ``read`` request; a ``read`` grant does not
    satisfy ``write``."""
    if requested == "read":
        return grant_mode in ("read", "write")
    return grant_mode == "write"


def list_grants() -> list[dict]:
    return _load_grants()


def grant_for(path, mode: str = "read") -> Optional[dict]:
    """The first grant whose (resolved) root contains ``path`` and whose mode
    satisfies ``mode``. ``None`` if no grant covers it (caller raises ``NeedsAccess``)."""
    resolved = Path(path).resolve()
    for g in _load_grants():
        root = Path(g["root"])
        if _contained(resolved, root) and _mode_satisfies(g["mode"], mode):
            return g
    return None


def add_grant(root, mode: str) -> dict:
    """Persist a grant for ``root`` (stored symlink-resolved). Idempotent: if a
    grant of a satisfying mode already contains ``root``, return that grant instead
    of nesting a duplicate."""
    resolved_root = Path(root).resolve()
    for g in _load_grants():
        existing = Path(g["root"])
        if _contained(resolved_root, existing) and _mode_satisfies(g["mode"], mode):
            return g
    grant = {
        "id": "grt_" + uuid.uuid4().hex,
        "root": str(resolved_root),
        "mode": mode,
        "granted_at": _now_iso(),
        "last_used": _now_iso(),
    }
    grants = _load_grants()
    grants.append(grant)
    _save_grants(grants)
    return grant


def revoke_grant(grant_id: str) -> bool:
    grants = _load_grants()
    kept = [g for g in grants if g.get("id") != grant_id]
    if len(kept) == len(grants):
        return False
    _save_grants(kept)
    return True


def touch_last_used(grant_id: str) -> None:
    grants = _load_grants()
    changed = False
    for g in grants:
        if g.get("id") == grant_id:
            g["last_used"] = _now_iso()
            changed = True
    if changed:
        _save_grants(grants)
