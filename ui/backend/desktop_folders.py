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

from pydantic import BaseModel, Field

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


def is_forbidden_root(root) -> bool:
    """A root the server refuses to grant even if the user picked it — a sensitive
    system directory (``.ssh``/``.aws``/``.kube``/``.git`` anywhere in the path).
    The native picker makes the user choose the root, but this is a server-side
    backstop against a bad or spoofed path reaching POST /grant."""
    return bool(set(Path(root).resolve().parts) & DENY_DIR_SEGMENTS)


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


# ── read primitives (contain → deny-list → caps → scrub → audit) ────────────────
#
# These are the safe accessors the four tools (PR 3) call. Each resolves a grant
# or raises NeedsAccess (which a tool turns into the consent-pause contract),
# contains the path, applies the deny-list and caps, scrubs every byte through
# the app-wide `sanitize_observation` chokepoint (redact_prose + entropy) before
# it can reach the LLM, and records a `folder.read` audit row carrying only
# {root, rel_path, bytes} — never content.

# grant/revoke are audited at the endpoint layer (routers/desktop_folders.py),
# where the human action actually happens; grant CRUD above stays side-effect-free
# and idempotent so the boundary can be unit-tested without a database.

_LIST_ENTRY_CAP = 2000          # max directory entries returned in one listing
_SEARCH_MATCH_CAP = 200         # max search hits returned in one call
_SEARCH_SNIPPET_CAP = 400       # per-snippet soft size for sanitize_observation


def _rel(resolved: Path, root: Path) -> str:
    parts = resolved.parts[len(root.parts):]
    return "/".join(parts)


def _audit_read(resolved: Path, root: Path, *, bytes_: int = 0, subject: str = "",
                extra: Optional[dict] = None) -> None:
    """Record a folder.read row. Never raises (audit.emit swallows its own errors)."""
    try:
        import audit
        payload = {"root": str(root), "rel_path": _rel(resolved, root), "bytes": bytes_}
        if extra:
            payload.update(extra)
        audit.emit(
            audit.EventType.FOLDER_READ,
            actor_type="agent",
            subject=subject or _rel(resolved, root),
            payload=payload,
        )
    except Exception:
        # Audit must never break a read; a logging gap is preferable to an outage.
        pass


def _lexically_in_any_grant(path, mode: str) -> bool:
    """True if the path *as written* (lexically absolute, symlinks NOT followed)
    sits inside a granted root of a satisfying mode. Used to tell an escape out of
    a held grant (deny) apart from a genuinely ungranted path (prompt)."""
    lex = Path(os.path.abspath(path))
    return any(
        _mode_satisfies(g["mode"], mode) and _contained(lex, Path(g["root"]))
        for g in _load_grants()
    )


def _require_read_grant(path) -> dict:
    """Resolve the grant for a read, or raise. A path that lexically sits inside a
    held grant but resolves outside it (``..`` or a symlink escape) is DENIED, not
    turned into a consent prompt — the agent tried to leave a folder it was given."""
    grant = grant_for(path, "read")
    if grant is not None:
        return grant
    if _lexically_in_any_grant(path, "read"):
        raise AccessDenied(str(path), "outside_grant")
    raise NeedsAccess(str(path), "read")


def read_file_contained(path) -> str:
    """Return a file's contents, redacted + sanitized + capped, or raise.

    Raises NeedsAccess (no grant), AccessDenied (escape / deny-list / caps)."""
    grant = _require_read_grant(path)
    root = Path(grant["root"])
    resolved = contain_path(path, root)
    if is_denied(resolved, root):
        raise AccessDenied(str(path), "deny_list")
    within_caps(resolved)                      # size + binary gate
    raw = resolved.read_text(errors="replace")
    from observation_sanitizer import sanitize_observation
    safe = sanitize_observation(raw, MAX_FILE_BYTES)
    _audit_read(resolved, root, bytes_=len(raw))
    touch_last_used(grant["id"])
    return safe


def list_folder_contained(path) -> dict:
    """Shallow directory listing within a grant, deny-list filtered and capped.

    Returns {root, path, entries:[{name, type}]}. The agent recurses by listing
    subdirectories; a shallow call keeps each response bounded."""
    grant = _require_read_grant(path)
    root = Path(grant["root"])
    resolved = contain_path(path, root)
    if not resolved.is_dir():
        raise AccessDenied(str(path), "not_a_directory")
    entries = []
    for child in sorted(resolved.iterdir(), key=lambda p: p.name):
        if is_denied(child, root):
            continue                            # hide deny-listed entries entirely
        entries.append({"name": child.name, "type": "dir" if child.is_dir() else "file"})
        if len(entries) >= _LIST_ENTRY_CAP:
            break
    _audit_read(resolved, root, subject=_rel(resolved, root),
                extra={"entries": len(entries)})
    touch_last_used(grant["id"])
    return {"root": str(root), "path": _rel(resolved, root), "entries": entries}


def search_files_contained(root, pattern: str) -> list[dict]:
    """grep-like search across a grant. Skips deny-listed, oversized and binary
    files; snippets are sanitized. Returns [{file, line, text}], capped."""
    grant = _require_read_grant(root)
    groot = Path(grant["root"])
    resolved_root = contain_path(root, groot)
    from observation_sanitizer import sanitize_observation
    needle = (pattern or "").lower()
    matches: list[dict] = []
    scanned = 0
    for p in sorted(resolved_root.rglob("*")):
        if len(matches) >= _SEARCH_MATCH_CAP:
            break
        if not p.is_file() or is_denied(p, groot):
            continue
        if not _contained(p.resolve(), groot):
            continue                            # symlink escaping the grant — never read it
        try:
            within_caps(p)                      # skip oversized/binary quietly
        except AccessDenied:
            continue
        scanned += 1
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if needle in line.lower():
                matches.append({
                    "file": _rel(p, groot),
                    "line": i,
                    "text": sanitize_observation(line, _SEARCH_SNIPPET_CAP),
                })
                if len(matches) >= _SEARCH_MATCH_CAP:
                    break
    _audit_read(resolved_root, groot, subject=f"search:{pattern}",
                extra={"pattern": pattern, "files_scanned": scanned, "matches": len(matches)})
    touch_last_used(grant["id"])
    return matches


# ── the bridge: find_source_for_workload ────────────────────────────────────────
#
# Links a running resource in the cluster (Deployment/api-gateway) to the local
# file(s) that define it, by reusing the GitOps (kind, name) index over local
# files instead of an in-memory tarball. Ambiguity returns a candidate list —
# never a guess (design §3.3).

_INDEX_FILE_CAP = 5000          # stop walking a pathological repo


def _local_repo_files(root: Path):
    """Adapt a granted local folder to gitops.index's RepoFile list: every
    deny-list-clear YAML under the root, path relative to the root."""
    from gitops.index import RepoFile
    out = []
    for p in root.rglob("*"):
        if len(out) >= _INDEX_FILE_CAP:
            break
        if p.suffix not in (".yaml", ".yml") or not p.is_file():
            continue
        if is_denied(p, root):
            continue
        if not _contained(p.resolve(), root):
            continue                            # symlink escaping the grant — never index it
        try:
            out.append(RepoFile(path=str(p.relative_to(root)), text=p.read_text(errors="replace")))
        except OSError:
            continue
    return out


def find_source_for_workload(kind: str, name: str, namespace: Optional[str] = None) -> list[dict]:
    """Which local file(s) define this cluster resource? Searches every granted
    read root. Raises NeedsAccess if no folder is granted yet."""
    grants = [g for g in _load_grants() if _mode_satisfies(g["mode"], "read")]
    if not grants:
        raise NeedsAccess(f"<workload {kind}/{name}>", "read", f"locate the source manifest for {kind}/{name}")
    from gitops.index import build_index
    candidates: list[dict] = []
    for g in grants:
        root = Path(g["root"])
        index = build_index(_local_repo_files(root))
        for m in index.get((kind, name), []):
            if namespace and m.namespace not in (None, namespace):
                continue
            candidates.append({
                "root": str(root),
                "file": m.file_path,
                "doc_index": m.doc_index,
                "namespace": m.namespace,
            })
        _audit_read(root, root, subject=f"find_source:{kind}/{name}",
                    extra={"kind": kind, "name": name, "candidates": len(candidates)})
        touch_last_used(g["id"])
    return candidates


# ── tools (desktop-only ToolDefs) ────────────────────────────────────────────────
#
# Handlers are (params, ctx) -> dict, matching the registry contract. They turn
# NeedsAccess into the consent-pause contract ({needs_access: …}) and AccessDenied
# into a normal tool error. ToolDef construction + registration is lazy (inside
# register_desktop_tools) so importing this module never requires the mcp package.

class ReadFileInput(BaseModel):
    path: str = Field(..., description="Absolute path to a file inside a granted folder.")
    reason: Optional[str] = Field(None, description="Why the file is needed (shown to the user on a consent prompt).")


class ListFolderInput(BaseModel):
    path: str = Field(..., description="Absolute path to a directory inside a granted folder.")


class SearchFilesInput(BaseModel):
    root: str = Field(..., description="Absolute path to a granted folder to search under.")
    pattern: str = Field(..., description="Case-insensitive substring to search for.")


class FindSourceForWorkloadInput(BaseModel):
    kind: str = Field(..., description="Kubernetes kind, e.g. Deployment.")
    name: str = Field(..., description="Resource name, e.g. api-gateway.")
    namespace: Optional[str] = Field(None, description="Optional namespace to disambiguate.")


def _needs(exc: "NeedsAccess", fallback_reason: str) -> dict:
    return {"needs_access": {"path": exc.path, "mode": exc.mode, "reason": exc.reason or fallback_reason}}


def _handle_read_file(params: dict, ctx=None) -> dict:
    try:
        content = read_file_contained(params["path"])
        return {"success": True, "content": content}
    except NeedsAccess as na:
        na.reason = na.reason or params.get("reason", "")
        return _needs(na, "read a file to continue the investigation")
    except AccessDenied as ad:
        return {"success": False, "error": f"denied: {ad.reason}", "path": ad.path}


def _handle_list_folder(params: dict, ctx=None) -> dict:
    try:
        return {"success": True, **list_folder_contained(params["path"])}
    except NeedsAccess as na:
        return _needs(na, "list a folder to continue the investigation")
    except AccessDenied as ad:
        return {"success": False, "error": f"denied: {ad.reason}", "path": ad.path}


def _handle_search_files(params: dict, ctx=None) -> dict:
    try:
        return {"success": True, "matches": search_files_contained(params["root"], params["pattern"])}
    except NeedsAccess as na:
        return _needs(na, "search a folder to continue the investigation")
    except AccessDenied as ad:
        return {"success": False, "error": f"denied: {ad.reason}", "path": ad.path}


def _handle_find_source_for_workload(params: dict, ctx=None) -> dict:
    try:
        cands = find_source_for_workload(params["kind"], params["name"], params.get("namespace"))
        return {"success": True, "candidates": cands}
    except NeedsAccess as na:
        return _needs(na, f"locate the source for {params.get('kind')}/{params.get('name')}")


def register_desktop_tools() -> None:
    """Register the four read tools into the shared registry. Called once at
    startup ONLY in desktop mode (main.py, inside `if DESKTOP_MODE:`). In server
    mode this is never called, so the tools are absent from every surface."""
    import tool_registry as tr

    surfaces = frozenset({"react", "chat"})
    defs = [
        tr.ToolDef(
            name="read_file", handler=_handle_read_file, schema=ReadFileInput,
            description=("Read a file from a folder the user has granted access to. Redacted, "
                         "size-capped. If the path isn't granted yet, returns needs_access and the "
                         "user is prompted to pick the folder."),
            category="local_folder", surfaces=surfaces,
        ),
        tr.ToolDef(
            name="list_folder", handler=_handle_list_folder, schema=ListFolderInput,
            description="List a directory inside a granted folder (deny-listed entries hidden).",
            category="local_folder", surfaces=surfaces,
        ),
        tr.ToolDef(
            name="search_files", handler=_handle_search_files, schema=SearchFilesInput,
            description="grep-like search across a granted folder; matching snippets are redacted.",
            category="local_folder", surfaces=surfaces,
        ),
        tr.ToolDef(
            name="find_source_for_workload", handler=_handle_find_source_for_workload,
            schema=FindSourceForWorkloadInput,
            description=("Find the local manifest file(s) that define a cluster resource, given its "
                         "kind and name. Returns candidates (a list on ambiguity, never a guess)."),
            category="local_folder", surfaces=surfaces,
        ),
    ]
    for d in defs:
        tr.register_tool(d)
