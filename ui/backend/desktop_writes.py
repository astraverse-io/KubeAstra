"""Desktop write boundary — the security core for local edits (Phase 2, PR 1).

Phase 1's ``desktop_folders`` made *reads* safe. This module does the same for
*writes*, the dangerous direction. The agent runs as the user with no OS sandbox
and the model is assumed hostile mid-run (prompt injection), so every write path
is gated here and re-checked at apply time, not just when the edit is proposed:

1. **Write grant** — a ``read`` grant never satisfies a write. No grant raises
   ``NeedsAccess(mode="write")`` so the user is prompted to "Allow write".
2. **Target** — contained in the grant (symlinks and ``..`` resolved), never a
   symlink itself, not deny-listed (``.git/`` included, which blocks git hooks),
   ``.yaml``/``.yml`` only (design §5: K8s-native scope), parent must exist.
3. **Edits** — exact search/replace, each anchor matching exactly once. The model
   only ever saw *redacted* file contents, so a full-content rewrite would write
   ``***redacted***`` over the user's real secrets. Anything carrying a
   redaction/truncation marker is refused, and bytes the model didn't target
   keep their real value (spec D1).
4. **Apply** — single-use, TTL'd token; the file must still hash to what was
   previewed (TOCTOU); the write is atomic (same-dir temp + fsync + replace).

Files are read and written as exact UTF-8 bytes: line endings (CRLF) survive an
edit, and a file that isn't valid UTF-8 is refused rather than silently mangled.

See internal_docs/features/DESKTOP_AGENT_PHASE2_SPEC.md §1–2 (PR 1).
"""

from __future__ import annotations

import hashlib
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import desktop_folders as df

PENDING_WRITE_TTL_SECONDS = 900          # 15 min, matching GitOps previews
WRITABLE_SUFFIXES = (".yaml", ".yml")
_NEW_FILE_MODE = 0o644

# Markers the read path can leave in what the model saw (redaction_kv,
# redaction_entropy, truncation). Their presence in an edit means the model is
# echoing a redacted view back — never the real bytes.
_REDACTION_MARKERS = (
    "***redacted",          # ***redacted*** and ***redacted (private key)***
    "<REDACTED:",
    "… [truncated,",
)


class WriteRefused(Exception):
    """A write was refused by the boundary. ``reason`` is a stable machine code."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"write refused ({reason}){': ' + detail if detail else ''}")


# ── grant ─────────────────────────────────────────────────────────────────────

def require_write_grant(path) -> dict:
    """The write grant covering ``path``, or raise.

    A path that lexically sits inside a held write grant but resolves outside it
    is an escape attempt → ``AccessDenied``, not a consent prompt."""
    grant = df.grant_for(path, "write")
    if grant is not None:
        return grant
    if df._lexically_in_any_grant(path, "write"):
        raise df.AccessDenied(str(path), "outside_grant")
    # Report the normalized path so the consent prompt shows where the write
    # would really land (`infra/../x` is not under infra).
    raise df.NeedsAccess(os.path.abspath(path), "write")


# ── target resolution ─────────────────────────────────────────────────────────

def resolve_write_target(path, grant: dict) -> tuple[Path, bool]:
    """Validate a write target inside ``grant``. Returns ``(resolved, exists)``.

    Order matters for clear reasons: symlink → containment → deny-list → type →
    shape (file vs dir, parent exists)."""
    root = Path(grant["root"])
    lexical = Path(os.path.abspath(path))

    # Never write through a symlink, even one that resolves inside the grant:
    # replacing it would either follow it (escape) or silently swap a link for
    # a file. Both are surprises the user didn't approve.
    if lexical.is_symlink():
        raise WriteRefused("symlink_target", str(path))

    exists = lexical.exists()
    if exists:
        resolved = df.contain_path(lexical, root)
    else:
        # New file: its parent must already exist inside the grant; the file
        # itself can't be resolved yet, so contain the parent and re-join.
        parent = lexical.parent
        if not parent.is_dir():
            raise WriteRefused("parent_missing", str(parent))
        resolved = df.contain_path(parent, root) / lexical.name

    if df.is_denied(resolved, root):
        raise WriteRefused("deny_list", str(path))
    if resolved.suffix.lower() not in WRITABLE_SUFFIXES:
        raise WriteRefused("unsupported_type", resolved.suffix or resolved.name)
    if exists and not resolved.is_file():
        raise WriteRefused("not_a_file", str(path))
    return resolved, exists


# ── exact read ────────────────────────────────────────────────────────────────

def read_text_exact(path: Path) -> str:
    """File contents as str with line endings preserved (no newline translation).
    Refuses non-UTF-8 so an edit can never mangle bytes it can't represent."""
    try:
        return Path(path).read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise WriteRefused("not_utf8", str(path))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── edits ─────────────────────────────────────────────────────────────────────

def contains_redaction_marker(text: str) -> bool:
    return any(m in (text or "") for m in _REDACTION_MARKERS)


def apply_edits(before: str, edits: list[dict]) -> str:
    """Apply exact search/replace edits in order. Each ``old`` must occur exactly
    once in the text as it stands at that step. Returns the edited text."""
    if not edits:
        raise WriteRefused("no_change", "no edits supplied")
    text = before
    for i, e in enumerate(edits):
        old = e.get("old") or ""
        new = e.get("new") if e.get("new") is not None else ""
        if contains_redaction_marker(old) or contains_redaction_marker(new):
            raise WriteRefused("redaction_marker", f"edit {i}")
        if not old:
            raise WriteRefused("edit_not_found", f"edit {i}: empty anchor")
        count = text.count(old)
        if count == 0:
            raise WriteRefused("edit_not_found", f"edit {i}")
        if count > 1:
            raise WriteRefused("edit_ambiguous", f"edit {i}: anchor matches {count} times")
        text = text.replace(old, new, 1)
    if text == before:
        raise WriteRefused("no_change", "edits leave the file unchanged")
    return text


# ── pending writes (mirrors gitops.store.PreviewStore) ────────────────────────

@dataclass
class PendingWrite:
    token: str
    root: str
    rel_path: str
    abs_path: str
    before: Optional[str]          # None for a new file
    after: str
    before_sha: Optional[str]
    created: bool
    diff: str
    validation: dict
    session_id: Optional[str]
    created_at: float
    expires_at: float
    extra: dict = field(default_factory=dict)


def new_pending_write(*, root, rel_path, abs_path, before, after, created, diff,
                      validation, session_id) -> PendingWrite:
    now = time.time()
    return PendingWrite(
        token="pwr_" + secrets.token_urlsafe(16),
        root=str(root), rel_path=rel_path, abs_path=str(abs_path),
        before=before, after=after,
        before_sha=None if before is None else sha256_text(before),
        created=created, diff=diff, validation=validation, session_id=session_id,
        created_at=now, expires_at=now + PENDING_WRITE_TTL_SECONDS,
    )


class PendingWriteStore:
    """In-memory, single-use, TTL'd. Single-process by design: desktop mode is
    one backend process on the user's laptop."""

    def __init__(self):
        self._items: dict[str, PendingWrite] = {}
        self._lock = threading.Lock()

    def _purge_locked(self):
        now = time.time()
        for t in [t for t, p in self._items.items() if p.expires_at < now]:
            self._items.pop(t, None)

    def put(self, pw: PendingWrite) -> None:
        with self._lock:
            self._purge_locked()
            self._items[pw.token] = pw

    def get(self, token: str) -> Optional[PendingWrite]:
        with self._lock:
            self._purge_locked()
            return self._items.get(token)

    def pop(self, token: str) -> Optional[PendingWrite]:
        with self._lock:
            self._purge_locked()
            return self._items.pop(token, None)


pending_write_store = PendingWriteStore()


# ── atomic write ──────────────────────────────────────────────────────────────

def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(target: Path, text: str, *, expected_sha: Optional[str]) -> None:
    """Write ``text`` to ``target`` atomically.

    ``expected_sha`` is the sha256 of the contents the user approved a diff
    against; the file must still hash to it (else ``file_changed``). ``None``
    means a new file, which must not exist (else ``file_exists``).

    The temp file lives in the target's own directory so ``os.replace`` is a
    same-filesystem atomic rename; a crash leaves either the old or the new file,
    never a half-written one."""
    target = Path(target)
    data = text.encode("utf-8")
    if len(data) > df.MAX_FILE_BYTES:
        raise WriteRefused("too_large", f"{len(data)} bytes")
    if target.is_symlink():
        raise WriteRefused("symlink_target", str(target))

    if expected_sha is None:
        if os.path.lexists(target):
            raise WriteRefused("file_exists", str(target))
        mode = _NEW_FILE_MODE
    else:
        if not target.is_file():
            raise WriteRefused("file_changed", "file no longer exists")
        if sha256_text(read_text_exact(target)) != expected_sha:
            raise WriteRefused("file_changed", str(target))
        mode = os.stat(target).st_mode & 0o7777

    directory = target.parent
    fd, tmp_name = tempfile.mkstemp(prefix=".kubeastra-tmp-", dir=str(directory))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        if expected_sha is None:
            # No-clobber create: link() fails if the name appeared meanwhile.
            try:
                os.link(tmp, target)
            except FileExistsError:
                raise WriteRefused("file_exists", str(target))
            except OSError:
                # Filesystem without hard links: best-effort re-check + replace.
                if os.path.lexists(target):
                    raise WriteRefused("file_exists", str(target))
                os.replace(tmp, target)
        else:
            os.replace(tmp, target)
        _fsync_dir(directory)
    finally:
        if tmp.exists():
            tmp.unlink()
