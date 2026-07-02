"""Ansible-aware chunkers — one per file_type the dispatcher sends here.

Design notes
------------
- Every chunker conforms to the ``ChunkerFn`` signature in ``chunking.py``:
  ``(content, metadata, max_tokens, overlap_tokens) -> list[Chunk]``.
- Each chunker is best-effort. If YAML parsing fails (Ansible's Jinja
  expressions in unquoted positions sometimes do), we fall through to
  a fixed-window text chunker so the file is still indexed — just less
  structurally.
- Module-name extraction (§11.4 of the plan) uses a denylist of Ansible
  task keywords so we don't mis-identify keywords like ``block`` or
  ``vars`` as the module. FQCN-shaped keys (containing a ``.``) are
  preferred over bare names so ``kubernetes.core.k8s_info`` beats
  ``k8s_info`` when both could match.
- For ``block:``/``rescue:`` wrappers, the module field is set to the
  *inner* module (recursed) but the ``task_name`` stays the wrapper's
  name — that's what users will see in stack traces.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import yaml

from services.rag.chunking import Chunk, fallback_text_chunks

logger = logging.getLogger(__name__)


# Ansible task keywords — anything in this set is NOT a module name even
# if it's the first key in the dict. List built from the Ansible docs:
# https://docs.ansible.com/ansible/latest/reference_appendices/playbooks_keywords.html
_ANSIBLE_TASK_KEYWORDS: frozenset[str] = frozenset({
    # identity / display
    "name", "tags", "vars", "args", "environment",
    # control flow
    "when", "loop", "loop_control", "with_items", "with_dict", "with_list",
    "with_fileglob", "with_together", "with_indexed_items", "with_subelements",
    "until", "retries", "delay",
    # state tracking
    "register", "changed_when", "failed_when", "ignore_errors",
    "ignore_unreachable",
    # privilege escalation
    "become", "become_user", "become_method", "become_flags",
    # execution control
    "delegate_to", "delegate_facts", "run_once", "no_log", "throttle",
    "any_errors_fatal", "timeout", "async", "poll",
    "check_mode", "diff", "connection", "remote_user", "local_action",
    # error/group handling
    "block", "rescue", "always",
    # handler-related
    "notify", "listen",
    # play-level (appear in tasks rarely but include for safety)
    "module_defaults", "collections",
})

# Ansible custom-module convention: each module file has a top-level
# ``DOCUMENTATION = r'''...'''`` (or ``"""..."""``) block. That block IS
# the semantic content we want to embed; the rest is implementation.
# The optional type annotation (``DOCUMENTATION: str = r'''...'''``) is
# uncommon but legal and shows up in newer modules — accept it too.
_DOC_BLOCK_RE = re.compile(
    r"^DOCUMENTATION(?:\s*:\s*\w+)?\s*=\s*\(?\s*r?[\"']{3}(.*?)[\"']{3}",
    re.DOTALL | re.MULTILINE,
)


# ── Per-task chunker (tasks/, handlers/) ─────────────────────────────────────

def chunk_ansible_tasks(
    content: str,
    metadata: Optional[dict] = None,
    max_tokens: int = 400,
    overlap_tokens: int = 60,
) -> list[Chunk]:
    """One chunk per top-level task. Suitable for ``tasks/*.yaml`` and
    ``handlers/*.yaml`` — both are lists of task dicts.

    On YAML parse failure (Jinja expressions in unquoted value positions
    are the usual culprit), falls back to fixed-window chunking and flags
    ``parse_error=true`` on the chunks so we can audit later.
    """
    metadata = metadata or {}
    role = metadata.get("role", "")
    role_subdir = metadata.get("role_subdir", "tasks")
    category = metadata.get("category", "")
    section_root = role or "(top)"

    data, parse_err = _try_yaml_load(content)
    if parse_err is not None:
        return fallback_text_chunks(
            content, max_tokens, overlap_tokens,
            section=f"{section_root} > {role_subdir}",
            extra={"parse_error": parse_err[:200], "role": role, "category": category},
        )
    if not isinstance(data, list):
        # Some files are a single task at top level (rare but seen) — wrap.
        if isinstance(data, dict):
            data = [data]
        else:
            return fallback_text_chunks(
                content, max_tokens, overlap_tokens,
                section=f"{section_root} > {role_subdir}",
                extra={"role": role, "category": category},
            )

    chunks: list[Chunk] = []
    for idx, task in enumerate(data):
        if not isinstance(task, dict):
            continue
        task_name = task.get("name") or f"(unnamed task {idx})"
        module = _extract_module(task) or ""
        has_rescue = "rescue" in task
        is_block = "block" in task
        text = _dump_yaml_task(task)
        chunks.append(Chunk(
            section=f"{section_root} > {role_subdir} > {task_name}",
            text=text,
            index=idx,
            extra={
                "task_name": str(task_name),
                "module": module,
                "has_rescue": has_rescue,
                "is_block": is_block,
                "role": role,
                "category": category,
                "kind": "task" if role_subdir == "tasks" else "handler",
            },
        ))
    return chunks


# ── Per-play chunker (playbooks/<group>/*.yaml) ──────────────────────────────

def chunk_ansible_playbook(
    content: str,
    metadata: Optional[dict] = None,
    max_tokens: int = 400,
    overlap_tokens: int = 60,
) -> list[Chunk]:
    """One chunk per play. A playbook file is a YAML list of play dicts."""
    metadata = metadata or {}
    play_group = metadata.get("play_group", "")
    section_root = play_group or "playbooks"

    data, parse_err = _try_yaml_load(content)
    if parse_err is not None:
        return fallback_text_chunks(
            content, max_tokens, overlap_tokens,
            section=section_root,
            extra={"parse_error": parse_err[:200], "play_group": play_group},
        )
    if not isinstance(data, list):
        return fallback_text_chunks(
            content, max_tokens, overlap_tokens,
            section=section_root,
            extra={"play_group": play_group},
        )

    chunks: list[Chunk] = []
    for idx, play in enumerate(data):
        if not isinstance(play, dict):
            continue
        play_name = play.get("name") or f"play[{idx}]"
        hosts = _stringify(play.get("hosts", "(no hosts)"))
        roles_list = _collect_role_names(play.get("roles"))
        text = _dump_yaml_task(play)
        chunks.append(Chunk(
            section=f"{section_root} > {play_name}",
            text=text,
            index=idx,
            extra={
                "play_name": str(play_name),
                "hosts": hosts,
                "roles_list": ",".join(roles_list),
                "play_group": play_group,
                "kind": "play",
            },
        ))
    return chunks


# ── Vars / defaults / meta / inventory chunker (whole file) ──────────────────

def chunk_ansible_vars(
    content: str,
    metadata: Optional[dict] = None,
    max_tokens: int = 400,
    overlap_tokens: int = 60,
) -> list[Chunk]:
    """Whole-file chunking (with fixed-window fallback when oversized) for
    files where structure is flat key→value: ``defaults/main.yaml``,
    ``vars/*.yaml``, ``meta/main.yaml``, ``inventory/<env>/...``."""
    metadata = metadata or {}
    role = metadata.get("role", "")
    role_subdir = metadata.get("role_subdir", "")
    env = metadata.get("environment", "")
    inv_kind = metadata.get("inventory_kind", "")
    parts = [p for p in [role, role_subdir, env, inv_kind] if p]
    section = " > ".join(parts) or "(top)"
    extra = {
        "kind": role_subdir or inv_kind or "vars",
        **{k: metadata[k] for k in ("role", "category", "environment") if k in metadata},
    }
    return fallback_text_chunks(
        content, max_tokens, overlap_tokens, section=section, extra=extra,
    )


# ── Jinja template chunker ───────────────────────────────────────────────────

def chunk_ansible_template(
    content: str,
    metadata: Optional[dict] = None,
    max_tokens: int = 400,
    overlap_tokens: int = 60,
) -> list[Chunk]:
    """Templates live next to roles. Whole-file chunking; sliding-window
    fallback when oversized."""
    metadata = metadata or {}
    role = metadata.get("role", "")
    path = metadata.get("path", "")
    template_name = path.rsplit("/", 1)[-1] if path else "template"
    section_root = f"{role} > templates" if role else "templates"
    return fallback_text_chunks(
        content, max_tokens, overlap_tokens,
        section=f"{section_root} > {template_name}",
        extra={
            "template_path": path,
            "role": role,
            "kind": "template",
        },
    )


# ── Custom Ansible module chunker (library/*.py) ─────────────────────────────

def chunk_ansible_module(
    content: str,
    metadata: Optional[dict] = None,
    max_tokens: int = 400,
    overlap_tokens: int = 60,
) -> list[Chunk]:
    """Extract the ``DOCUMENTATION`` YAML block from a custom Ansible
    module. That block is the module's self-documentation — module name,
    options, examples — which is what an error like
    ``Failed to import the required Python library`` should match against.
    Falls back to fixed-window over the Python source if no doc block is
    found.
    """
    metadata = metadata or {}
    module_name = metadata.get("module_name", "")
    section = f"library > {module_name}" if module_name else "library"

    m = _DOC_BLOCK_RE.search(content)
    if m:
        doc_text = m.group(1).strip()
        if doc_text:
            return [Chunk(
                section=section,
                text=doc_text,
                index=0,
                extra={
                    "module_name": module_name,
                    "kind": "custom_module",
                    "doc_extracted": True,
                },
            )]
    return fallback_text_chunks(
        content, max_tokens, overlap_tokens,
        section=section,
        extra={
            "module_name": module_name,
            "kind": "custom_module",
            "doc_extracted": False,
        },
    )


# ── Per-role aggregate chunker (synthetic Documents from §11.3 emitter) ──────

def chunk_role_aggregate(
    content: str,
    metadata: Optional[dict] = None,
    max_tokens: int = 400,
    overlap_tokens: int = 60,
) -> list[Chunk]:
    """The aggregate emitter pre-builds content sized to fit; this just
    wraps it as one chunk (or windows it if somehow oversized). The
    aggregate's purpose is to give the retriever a single dense vector
    for the whole role — splitting defeats that, so we double the budget
    before windowing kicks in."""
    metadata = metadata or {}
    role = metadata.get("role", "")
    category = metadata.get("category", "")
    section = f"{category} > {role}" if category and role else (role or "role")
    return fallback_text_chunks(
        content, max_tokens * 2, overlap_tokens,
        section=section,
        extra={
            "kind": "role_aggregate",
            "role": role,
            "category": category,
        },
    )


# ── Internals ────────────────────────────────────────────────────────────────

def _try_yaml_load(content: str) -> tuple[Any, Optional[str]]:
    """Best-effort YAML parse. Returns ``(data, None)`` on success or
    ``(None, error_message)`` on failure."""
    try:
        return yaml.safe_load(content), None
    except yaml.YAMLError as exc:
        return None, str(exc)


def _extract_module(task: dict) -> Optional[str]:
    """Find the module key in a task dict.

    Rules (§11.4):
      1. Skip known Ansible task keywords.
      2. Prefer FQCN-shaped keys (contain a ``.``).
      3. For ``block:`` wrappers, recurse into the first child task.
      4. Return ``None`` if nothing module-shaped is found.
    """
    # Recurse through block/rescue/always wrappers — they're not modules
    # themselves, but their children are.
    for wrapper_key in ("block", "rescue", "always"):
        children = task.get(wrapper_key)
        if isinstance(children, list):
            for inner in children:
                if isinstance(inner, dict):
                    m = _extract_module(inner)
                    if m:
                        return m
            # Block exists but no inner module found — fall through, the
            # outer task itself may still have a sibling action key.

    # Filter candidate keys: exclude keywords and non-string keys.
    candidates = [
        k for k in task.keys()
        if isinstance(k, str) and k not in _ANSIBLE_TASK_KEYWORDS
    ]
    if not candidates:
        return None

    # Prefer FQCN-shaped: ``kubernetes.core.k8s_info`` beats ``k8s_info``.
    fqcn = [k for k in candidates if "." in k and not k.startswith(".")]
    if fqcn:
        return fqcn[0]
    return candidates[0]


def _dump_yaml_task(obj: Any) -> str:
    """Serialize a parsed task/play back to YAML for the chunk body.

    Wraps single tasks in a list so the output stays a valid YAML
    document. Disables key sorting so the original order survives.
    """
    try:
        return yaml.safe_dump(
            [obj], default_flow_style=False, sort_keys=False, allow_unicode=True,
        )
    except Exception as exc:
        # Last resort: stringify. Shouldn't happen with safe_load output.
        logger.debug("yaml.safe_dump failed (%s); using repr fallback", exc)
        return repr(obj)


def _stringify(v: Any) -> str:
    if isinstance(v, list):
        return ",".join(str(x) for x in v)
    return str(v)


def _collect_role_names(roles: Any) -> list[str]:
    """Plays declare ``roles:`` either as a list of names or a list of
    dicts with a ``role:`` key. Normalize both shapes to a flat name
    list."""
    if not isinstance(roles, list):
        return []
    names: list[str] = []
    for r in roles:
        if isinstance(r, str):
            names.append(r)
        elif isinstance(r, dict):
            n = r.get("role") or r.get("name")
            if n:
                names.append(str(n))
    return names
