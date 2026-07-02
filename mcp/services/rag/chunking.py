"""Markdown-aware document chunking.

Strategy:
  1. Split on top-level headers (``#``..``####``) — each section becomes a
     candidate chunk. Section path is preserved for citations (e.g.
     "Runbooks > Pod CrashLoopBackOff > Fix steps").
  2. If a section is larger than the max-token budget, slice it into
     overlapping windows.
  3. Documents with no headers fall through to fixed-window chunking on
     the whole content.

Token estimation is a deliberately coarse word-count × 1.3 — close enough
for English to keep us under the embedding model's input limit (the
``all-MiniLM-L6-v2`` ceiling is 512 tokens; we default to 400 to leave
headroom for the prepended section path).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional


_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
_WORDS_TO_TOKENS = 1.3  # rough English heuristic


@dataclass
class Chunk:
    section: str        # breadcrumb path, e.g. "Runbooks > CrashLoopBackOff"
    text: str           # the chunk body
    index: int          # position within the source doc, 0-based
    # Optional chunker-specific payload fields. Merged into the Qdrant
    # payload by the ingestion orchestrator. Examples for Ansible:
    # task_name, module, has_rescue, play_name, hosts, roles_list,
    # template_path, module_name, kind.
    extra: dict = field(default_factory=dict)


# Chunker function signature. Every chunker (markdown, ansible-task,
# ansible-playbook, etc.) conforms to this so ``pick_chunker`` can swap
# them transparently. ``metadata`` is the originating Document's metadata
# dict; chunkers that don't need it ignore the argument.
ChunkerFn = Callable[[str, dict, int, int], list["Chunk"]]


def chunk_markdown(
    content: str,
    metadata: Optional[dict] = None,
    max_tokens: int = 400,
    overlap_tokens: int = 60,
) -> list[Chunk]:
    """Split a markdown document into chunks ≤ max_tokens.

    Empty/whitespace-only inputs return an empty list — callers should
    skip ingestion rather than embedding nothing. ``metadata`` is accepted
    to match the ``ChunkerFn`` signature but unused by the markdown
    chunker.
    """
    del metadata  # unused
    if not content or not content.strip():
        return []

    sections = _split_by_headers(content)
    if not sections:
        # No headers at all — chunk the whole thing as one anonymous section.
        sections = [("", content)]

    chunks: list[Chunk] = []
    idx = 0
    for header, body in sections:
        body = body.strip()
        if not body:
            continue
        for window in _windows(body, max_tokens, overlap_tokens):
            chunks.append(Chunk(section=header or "(top)", text=window, index=idx))
            idx += 1
    return chunks


# ── Internals ────────────────────────────────────────────────────────────────

def _split_by_headers(content: str) -> list[tuple[str, str]]:
    """Walk the markdown header regex and return [(breadcrumb, body), ...]."""
    matches = list(_HEADER_RE.finditer(content))
    if not matches:
        return []

    sections: list[tuple[str, str]] = []
    # Preamble (text before the first header) attached to "(top)".
    if matches[0].start() > 0:
        preamble = content[: matches[0].start()]
        if preamble.strip():
            sections.append(("(top)", preamble))

    # Track the running breadcrumb path by header level.
    stack: list[tuple[int, str]] = []

    for i, m in enumerate(matches):
        level = len(m.group(1))
        title = m.group(2).strip()

        # Pop entries deeper than or equal to this level — we're starting
        # a new sibling/parent at this level.
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        breadcrumb = " > ".join(t for _, t in stack)

        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[body_start:body_end]
        sections.append((breadcrumb, body))

    return sections


def fallback_text_chunks(
    content: str,
    max_tokens: int,
    overlap_tokens: int,
    *,
    section: str,
    extra: Optional[dict] = None,
) -> list[Chunk]:
    """Shared helper: emit one or more chunks for content with no obvious
    internal structure (Jinja templates, vars files, YAML that failed to
    parse). Single chunk if it fits the budget, sliding windows otherwise.

    Used by Ansible chunkers in ``chunking_ansible.py``.
    """
    base_extra = dict(extra or {})
    if not content or not content.strip():
        return []
    chunks: list[Chunk] = []
    for i, window in enumerate(_windows(content, max_tokens, overlap_tokens)):
        chunks.append(Chunk(
            section=section,
            text=window,
            index=i,
            extra=dict(base_extra),
        ))
    return chunks


def pick_chunker(doc) -> ChunkerFn:
    """Pick the chunker function for a Document based on its metadata.

    Looks at ``doc.metadata["file_type"]`` (set by ``LocalPathSource``) and,
    for YAML files, additional path-derived metadata (``role_subdir``,
    ``play_group``, ``environment``) to disambiguate task lists vs.
    playbooks vs. vars vs. inventory.

    Returns a ``ChunkerFn``. Unknown file types fall back to
    ``chunk_markdown``, which handles plain text gracefully via its
    no-headers path.
    """
    # Lazy import — chunking_ansible imports back from this module for
    # ``Chunk``, ``_windows``, and ``fallback_text_chunks``.
    from services.rag.chunking_ansible import (
        chunk_ansible_module,
        chunk_ansible_playbook,
        chunk_ansible_tasks,
        chunk_ansible_template,
        chunk_ansible_vars,
        chunk_role_aggregate,
    )

    meta = getattr(doc, "metadata", {}) or {}
    file_type = meta.get("file_type", "")

    if file_type == "markdown":
        return chunk_markdown

    if file_type == "yaml":
        role_subdir = meta.get("role_subdir", "")
        if role_subdir in ("tasks", "handlers"):
            return chunk_ansible_tasks
        if role_subdir in ("defaults", "vars", "meta"):
            return chunk_ansible_vars
        if "play_group" in meta:
            return chunk_ansible_playbook
        if "environment" in meta:
            # inventory/<env>/... — host/group vars, hosts files
            return chunk_ansible_vars
        # Generic YAML (top-level files, files in unrecognized layouts)
        return chunk_ansible_vars

    if file_type == "jinja":
        return chunk_ansible_template

    if file_type == "ansible_module":
        return chunk_ansible_module

    if file_type == "role_aggregate":
        return chunk_role_aggregate

    # Unknown or unset — markdown chunker handles unstructured text fine.
    return chunk_markdown


def _windows(text: str, max_tokens: int, overlap_tokens: int):
    """Yield windows of the body. Whole text if it fits; sliding windows
    otherwise. Boundaries are word-aligned, not character-aligned."""
    words = text.split()
    if not words:
        return

    est_tokens = int(len(words) * _WORDS_TO_TOKENS)
    if est_tokens <= max_tokens:
        yield text
        return

    max_words = max(1, int(max_tokens / _WORDS_TO_TOKENS))
    overlap_words = max(0, min(max_words - 1, int(overlap_tokens / _WORDS_TO_TOKENS)))
    step = max(1, max_words - overlap_words)

    i = 0
    while i < len(words):
        yield " ".join(words[i: i + max_words])
        if i + max_words >= len(words):
            break
        i += step
