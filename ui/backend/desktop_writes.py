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

import difflib
import hashlib
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

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


# ── what the model saw: lines it may not touch ────────────────────────────────
#
# The model reads files through sanitize_observation, so some lines reach it
# redacted. An edit that changes such a line would either corrupt a secret it
# never saw, or — via a partial anchor like `old: "password: "` — surface the
# real value in the diff's "-" line. Those lines are off limits to the agent.

_PEM_PRIVATE_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE)
_PEM_PRIVATE_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE)


def _redacted_line_indexes(text: str) -> set[int]:
    """0-based indexes of lines the model saw redacted: every line of a
    private-key PEM block (redacted whole, even unterminated), plus any line the
    sanitizer alters on its own."""
    from observation_sanitizer import sanitize_observation
    lines = text.splitlines()
    hidden: set[int] = set()
    in_pem = False
    for i, line in enumerate(lines):
        if not in_pem and _PEM_PRIVATE_BEGIN.search(line):
            in_pem = True
        if in_pem:
            hidden.add(i)
            if _PEM_PRIVATE_END.search(line):
                in_pem = False
            continue
        if sanitize_observation(line, len(line) + 1) != line:
            hidden.add(i)
    return hidden


def _touches_hidden_lines(before: str, after: str) -> bool:
    hidden = _redacted_line_indexes(before)
    if not hidden:
        return False
    sm = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines(), autojunk=False)
    for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
        if tag in ("replace", "delete") and any(i in hidden for i in range(i1, i2)):
            return True
        # An insertion strictly inside a hidden run (e.g. into a PEM block).
        if tag == "insert" and (i1 - 1) in hidden and i1 in hidden:
            return True
    return False


# ── propose_file_edit (desktop-only tool) ─────────────────────────────────────
#
# Never writes. Resolves a write grant, applies exact edits in memory, validates,
# and parks a single-use pending write for the human to approve via
# POST /api/desktop/files/apply. The model gets a diff computed from the
# sanitized views; the real diff is served only to the local approval UI.

MAX_INVALID_ATTEMPTS = 3
_ATTEMPT_KEYS_CAP = 1000
_invalid_attempts: dict[tuple, int] = {}
_attempts_lock = threading.Lock()
_DIFF_CAP = 20_000

# Refusals that are the model's mistake and count toward the self-correct cap.
# Security refusals (deny_list, symlink_target, outside_grant …) are final and
# don't count — retrying them can't succeed.
_COUNTED_REASONS = frozenset({
    "edit_not_found", "edit_ambiguous", "no_change", "redaction_marker",
    "use_edits", "file_missing", "touches_redacted_line",
})

_HINTS = {
    "edit_not_found": "Each `old` must be copied exactly from the file as read_file shows it, "
                      "whitespace included. Re-read the file and retry.",
    "edit_ambiguous": "That `old` text appears more than once; include more surrounding lines "
                      "so it matches exactly once.",
    "redaction_marker": "Redacted values (***redacted***, <REDACTED:…>) are not the real file "
                        "contents; never put them in `old`, `new` or `content`.",
    "touches_redacted_line": "That edit changes a line whose real value you were shown redacted "
                             "(a secret). Leave it alone; tell the user to change it themselves.",
    "no_change": "The edit leaves the file unchanged.",
    "use_edits": "The file exists: change it with `edits` (exact search/replace), not `content`.",
    "file_missing": "The file doesn't exist: create it with `content`, or check the path "
                    "(find_source_for_workload / list_folder).",
    "unsupported_type": "Only .yaml / .yml files can be edited.",
    "deny_list": "That file is protected and can't be edited by the agent.",
    "symlink_target": "Symlinks can't be edited; edit the file they point to.",
    "parent_missing": "The folder for the new file doesn't exist.",
    "not_a_file": "That path is a directory.",
    "not_utf8": "The file isn't UTF-8 text and can't be edited safely.",
    "too_large": "The resulting file would be too large.",
}

_STOP_MESSAGE = ("Stop proposing edits to this file: {n} attempts failed. Report the failures to "
                 "the user and suggest the fix in your answer instead of retrying.")


class FileEdit(BaseModel):
    old: str = Field(..., description="Exact text currently in the file (copied from read_file), "
                                      "matching exactly once. Include neighbouring lines to make it unique.")
    new: str = Field(..., description="Replacement text.")


class ProposeFileEditInput(BaseModel):
    path: str = Field(..., description="Absolute path of a .yaml/.yml file inside a granted folder.")
    reason: str = Field(..., description="Why this change fixes the problem (shown to the user).")
    edits: list[FileEdit] = Field(default_factory=list,
                                  description="Exact search/replace edits for an existing file, applied in order.")
    content: Optional[str] = Field(None, description="Full content — ONLY when creating a new file.")


def _attempt_key(session_id, target: Path) -> tuple:
    return (session_id or "", str(target))


def _bump_attempts(key: tuple) -> int:
    with _attempts_lock:
        n = _invalid_attempts.get(key, 0) + 1
        _invalid_attempts[key] = n
        while len(_invalid_attempts) > _ATTEMPT_KEYS_CAP:
            _invalid_attempts.pop(next(iter(_invalid_attempts)))
        return n


def _reset_attempts(key: tuple) -> None:
    with _attempts_lock:
        _invalid_attempts.pop(key, None)


def _scrub(text: Optional[str], cap: int = 400) -> Optional[str]:
    if text is None:
        return None
    from observation_sanitizer import sanitize_observation
    return sanitize_observation(text, cap)


def _failed_attempt(key: tuple, result: dict, failures: list[dict]) -> dict:
    """Count a failed proposal; past the cap, turn it into a stop."""
    n = _bump_attempts(key)
    if n > MAX_INVALID_ATTEMPTS:
        return {"success": False, "stop": True, "failures": failures,
                "path": result.get("path"),
                "message": _STOP_MESSAGE.format(n=MAX_INVALID_ATTEMPTS)}
    result["attempts_remaining"] = MAX_INVALID_ATTEMPTS - n
    return result


def _refusal(reason: str, detail: str, path, key: Optional[tuple]) -> dict:
    out = {"success": False, "error": f"refused: {reason}", "path": str(path),
           "detail": _scrub(detail), "hint": _HINTS.get(reason, "")}
    if key is not None and reason in _COUNTED_REASONS:
        return _failed_attempt(key, out, [{"name": reason, "status": "fail", "detail": out["hint"]}])
    return out


def _handle_propose_file_edit(params: dict, ctx=None) -> dict:
    path = params["path"]
    reason = params.get("reason") or ""
    edits = params.get("edits") or []
    content = params.get("content")
    session_id = getattr(ctx, "session_id", None)

    try:
        grant = require_write_grant(path)
        target, exists = resolve_write_target(path, grant)
    except df.NeedsAccess as na:
        return {"needs_access": {"path": na.path, "mode": "write",
                                 "reason": reason or "edit a file to fix the problem"}}
    except df.AccessDenied as ad:
        return {"success": False, "error": f"denied: {ad.reason}", "path": ad.path}
    except WriteRefused as wr:
        return _refusal(wr.reason, wr.detail, path, None)

    root = Path(grant["root"])
    rel = df._rel(target, root)
    key = _attempt_key(session_id, target)

    try:
        if exists:
            if content is not None and not edits:
                raise WriteRefused("use_edits")
            before: Optional[str] = read_text_exact(target)
            after = apply_edits(before, edits)
            if _touches_hidden_lines(before, after):
                raise WriteRefused("touches_redacted_line")
        else:
            if edits:
                raise WriteRefused("file_missing")
            if not content:
                raise WriteRefused("no_change", "empty content")
            if contains_redaction_marker(content):
                raise WriteRefused("redaction_marker", "content")
            before, after = None, content
        if len(after.encode("utf-8")) > df.MAX_FILE_BYTES:
            raise WriteRefused("too_large")
    except WriteRefused as wr:
        return _refusal(wr.reason, wr.detail, rel, key)

    from desktop_validators import validate_edit
    from gitops.edit import unified_diff
    from observation_sanitizer import sanitize_observation

    validation = validate_edit(target, before, after, root=root).to_dict()
    validation["unvalidated_reason"] = _scrub(validation["unvalidated_reason"])
    for c in validation["checks"]:
        c["detail"] = _scrub(c["detail"])

    if not validation["ok"]:
        failures = [c for c in validation["checks"] if c["status"] == "fail"]
        result = {"success": False, "invalid": True, "path": rel, "failures": failures,
                  "checks": validation["checks"],
                  "message": "The edit failed validation and was not proposed. Fix the failures "
                             "and call propose_file_edit again."}
        return _failed_attempt(key, result, failures)

    _reset_attempts(key)
    real_diff = unified_diff(rel, before or "", after)
    # The model's diff is computed from the sanitized views — the same text it
    # read — so no secret context line can ride along.
    model_diff = sanitize_observation(
        unified_diff(rel, sanitize_observation(before or "", df.MAX_FILE_BYTES),
                     sanitize_observation(after, df.MAX_FILE_BYTES)),
        _DIFF_CAP,
    )
    pw = new_pending_write(root=root, rel_path=rel, abs_path=target, before=before, after=after,
                           created=not exists, diff=real_diff, validation=validation,
                           session_id=session_id)
    pw.extra.update({"model_diff": model_diff, "reason": reason})
    pending_write_store.put(pw)
    if before is not None:
        df._audit_read(target, root, bytes_=len(before), subject=f"propose_edit:{rel}")
    return {
        "success": True,
        "pending_write": {
            "token": pw.token, "path": rel, "root": str(root), "created": not exists,
            "reason": reason, "diff": model_diff, "validation": validation,
            "expires_at": pw.expires_at,
        },
        "message": "Edit proposed and validated. It is NOT written yet — the user must approve "
                   "it in the app before anything changes on disk.",
    }


def render_propose_observation(result) -> Optional[str]:
    """What the model sees from propose_file_edit. Never includes the approval
    token (the model has no use for it)."""
    if not isinstance(result, dict):
        return None
    if result.get("stop"):
        lines = [result.get("message", "")]
        lines += [f"- {f.get('name')}: {f.get('detail')}" for f in result.get("failures", [])]
        return "\n".join(lines)
    if result.get("invalid"):
        n = result.get("attempts_remaining")
        lines = [f"INVALID edit to {result.get('path')} — not proposed. "
                 f"{n} attempt(s) left before you must stop."]
        lines += [f"- {f.get('name')}: {f.get('detail')}" for f in result.get("failures", [])]
        return "\n".join(lines)
    if result.get("success") is False:
        text = f"propose_file_edit {result.get('error')}"
        if result.get("hint"):
            text += f"\n{result['hint']}"
        if result.get("attempts_remaining") is not None:
            text += f"\n{result['attempts_remaining']} attempt(s) left before you must stop."
        return text
    pw = result.get("pending_write")
    if not isinstance(pw, dict):
        return None
    v = pw.get("validation") or {}
    status = "validated" if v.get("validated") else f"UNVALIDATED ({v.get('unvalidated_reason')})"
    lines = [result.get("message", ""), f"file: {pw.get('path')} ({'new file' if pw.get('created') else 'edit'})",
             f"validation: {status}"]
    lines += [f"- {c.get('name')}: {c.get('status')}" + (f" — {c['detail']}" if c.get("detail") else "")
              for c in v.get("checks", [])]
    lines += ["diff:", pw.get("diff", "")]
    return "\n".join(lines)


def propose_file_edit_tooldef():
    import tool_registry as tr
    return tr.ToolDef(
        name="propose_file_edit", handler=_handle_propose_file_edit, schema=ProposeFileEditInput,
        description=("Propose a change to a .yaml/.yml file in a folder the user granted write access "
                     "to. Does NOT write: the change is validated (YAML, schema, policy, kustomize/helm "
                     "render) and shown to the user as a diff to approve. For an existing file pass "
                     "`edits` — exact search/replace pairs where `old` is copied verbatim from read_file "
                     "and matches once; pass `content` only to create a new file. Never use redacted "
                     "values. If validation fails, fix the reported problems and retry (max 3)."),
        category="local_folder", surfaces=frozenset({"react", "chat"}),
    )
