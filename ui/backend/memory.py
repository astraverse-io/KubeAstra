"""Per-user conversation memory (Phase 2.2).

Captures lightweight signals about what each user has been working on —
recent namespaces, resources, tools, and clusters — and renders them as
a short preamble injected into the LLM prompt on subsequent turns.

The intent is not to be a knowledge base; it's to be a tiny "you've
been working with X" reminder so the agent doesn't keep asking the user
to re-specify the same context across messages.

Capture point: every successful tool dispatch inside the ReAct loop.
The LLM has already disambiguated the user's natural language into
structured params (namespace=prod, pod_name=api-1, ...), so we get
high-quality entities essentially for free.

Storage: a single JSON row per session in SQLite (see db.user_memory).
No new infrastructure.

Design choices kept deliberately simple for v1:
- Top N=10 per category, sorted by recency.
- No explicit decay — older entries fall off naturally as new ones push in.
- Cap on absolute age (24h) applied at render time so a 3-day-old name
  doesn't follow you forever.
- Records every observation; merges on save by latest-wins for last_seen
  and sums count.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Optional

import db

logger = logging.getLogger(__name__)

# Top N entries kept per category.
_CAP_PER_CATEGORY = 10

# Drop entries older than this when rendering the preamble (still kept in
# storage in case they get refreshed, but don't pollute the prompt).
_MAX_AGE_SECONDS_FOR_PREAMBLE = 24 * 60 * 60  # 24 h

# Categories we track. Order matters for preamble display.
_CATEGORIES = ("namespaces", "resources", "tools", "clusters")

# Which keys in tool params map to which entity category.
# Multiple keys can feed the same category (e.g. pod_name, deployment_name,
# resource_name all are "resources").
_PARAM_TO_CATEGORY: dict[str, str] = {
    "namespace": "namespaces",
    "pod_name": "resources",
    "deployment_name": "resources",
    "resource_name": "resources",
    "workload_name": "resources",
    "service_name": "resources",
    "cluster_name": "clusters",
    "context_name": "clusters",
}

# Sentinel values we never want to record as "recent context" — too generic
# to be useful, would clutter the preamble.
_VALUE_BLOCKLIST = frozenset({
    "", "*", "all", "all-namespaces", "default", "none",
})


def record_tool_call(
    session_id: Optional[str],
    tool: str,
    params: dict,
) -> None:
    """Update the user's memory with entities from this tool invocation.

    Silent no-op when session_id is missing (anonymous calls) so memory
    is opt-in by virtue of having a session.
    """
    if not session_id:
        return
    if not tool:
        return

    try:
        entities = db.get_user_memory(session_id)
        now = time.time()

        # Always record the tool itself.
        _upsert(entities, "tools", tool, now)

        # Pull entities out of params using the static map.
        for key, value in (params or {}).items():
            cat = _PARAM_TO_CATEGORY.get(key)
            if not cat:
                continue
            for v in _flatten(value):
                if not isinstance(v, str):
                    continue
                v_norm = v.strip()
                if v_norm.lower() in _VALUE_BLOCKLIST:
                    continue
                _upsert(entities, cat, v_norm, now)

        db.save_user_memory(session_id, entities)
    except Exception as exc:
        # Memory capture must never break a tool call.
        logger.warning("memory: record_tool_call failed for session=%s: %s",
                       session_id, exc)


def build_memory_preamble(session_id: Optional[str]) -> str:
    """Return a short text block summarizing this user's recent context,
    suitable for prepending to the LLM prompt. Empty string if there's
    nothing useful to share (anonymous, no history, or only stale data)."""
    if not session_id:
        return ""

    try:
        entities = db.get_user_memory(session_id)
    except Exception as exc:
        logger.warning("memory: read failed for session=%s: %s", session_id, exc)
        return ""

    if not entities:
        return ""

    cutoff = time.time() - _MAX_AGE_SECONDS_FOR_PREAMBLE
    lines: list[str] = []

    for cat in _CATEGORIES:
        items = entities.get(cat) or []
        # Already stored sorted by last_seen desc; filter to fresh + take top.
        fresh = [item for item in items if (item.get("last_seen") or 0) >= cutoff]
        if not fresh:
            continue
        values = [item["value"] for item in fresh[:5]]
        label = {
            "namespaces": "Recent namespaces",
            "resources":  "Recent workloads",
            "tools":      "Recently used tools",
            "clusters":   "Recent clusters",
        }.get(cat, cat.title())
        lines.append(f"- {label}: {', '.join(values)}")

    if not lines:
        return ""

    return (
        "[User's recent context — use to disambiguate references like "
        "'the same', 'that pod', 'in prod' without re-asking]\n"
        + "\n".join(lines)
        + "\n"
    )


# ── Internal helpers ─────────────────────────────────────────────────────────

def _upsert(entities: dict, category: str, value: str, now: float) -> None:
    """Merge `value` into entities[category] using latest-wins + count++."""
    bucket: list[dict] = entities.setdefault(category, [])

    for entry in bucket:
        if entry.get("value") == value:
            entry["last_seen"] = now
            entry["count"] = int(entry.get("count", 0)) + 1
            break
    else:
        bucket.append({"value": value, "last_seen": now, "count": 1})

    # Sort by recency desc, then by count desc as a tiebreaker.
    bucket.sort(key=lambda e: (-(e.get("last_seen") or 0), -(e.get("count") or 0)))
    # Cap.
    if len(bucket) > _CAP_PER_CATEGORY:
        del bucket[_CAP_PER_CATEGORY:]


def _flatten(value: Any) -> Iterable:
    """Yield individual scalar values from a param value that might be a
    list, tuple, or scalar. Skips dicts and None."""
    if value is None:
        return
    if isinstance(value, (list, tuple, set)):
        for v in value:
            yield v
    elif isinstance(value, dict):
        return
    else:
        yield value
