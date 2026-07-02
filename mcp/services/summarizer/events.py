"""Summarizer for kubectl event lists.

Events arrive as a list of parsed dicts (see k8s.parsers.parse_events) with
keys like type, reason, message, object, count, last_seen.

Strategy (heuristic, deterministic, no LLM):
  1. Drop routine Normal-type events that don't aid diagnosis
     (Pulling, Pulled, Created, Started, Scheduled, SuccessfulCreate, ...).
  2. Cluster remaining events by (type, reason, object_kind) so 200 identical
     "BackOff restarting failed container" lines collapse to one entry with
     a count.
  3. Render as a tight text block the LLM can read directly.

Result is intentionally text (not structured) so it drops straight into a
prompt template alongside the other summaries.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import List

from config.settings import get_settings


# Normal-type reasons that are pure noise in a diagnosis context.
_NOISE_REASONS = frozenset({
    "Pulling", "Pulled", "Created", "Started", "Scheduled",
    "SuccessfulCreate", "SuccessfulDelete", "SuccessfulAttachVolume",
    "NodeReady", "Sync", "AddedInterface",
})


@dataclass
class EventsSummaryStats:
    events_in: int = 0
    events_out: int = 0
    noise_dropped: int = 0
    clusters: int = 0


@dataclass
class EventsSummaryResult:
    summary: str
    method: str  # "heuristic" | "none"
    stats: EventsSummaryStats


def summarize_events(events: List[dict]) -> EventsSummaryResult:
    """Cluster and de-noise a list of parsed event dicts."""
    settings = get_settings()
    stats = EventsSummaryStats(events_in=len(events))

    if not events:
        return EventsSummaryResult("", "none", stats)

    if not settings.enable_log_summarization:
        # Feature flag shared with logs summarizer.
        return EventsSummaryResult("", "none", stats)

    # Cluster.
    clusters: dict[tuple, dict] = defaultdict(
        lambda: {"count": 0, "latest_message": "", "last_seen": "", "objects": set()}
    )
    for ev in events:
        ev_type = (ev.get("type") or "").strip()
        reason = (ev.get("reason") or "").strip()
        message = (ev.get("message") or "").strip()
        obj = ev.get("object") or ev.get("involvedObject") or {}
        if isinstance(obj, dict):
            obj_kind = obj.get("kind", "")
            obj_name = obj.get("name", "")
        else:
            obj_kind, obj_name = "", str(obj)

        # Drop routine Normal noise.
        if ev_type == "Normal" and reason in _NOISE_REASONS:
            stats.noise_dropped += 1
            continue

        key = (ev_type, reason, obj_kind)
        c = clusters[key]
        c["count"] += int(ev.get("count", 1) or 1)
        c["latest_message"] = message or c["latest_message"]
        c["last_seen"] = ev.get("last_seen", "") or c["last_seen"]
        if obj_name:
            c["objects"].add(obj_name)

    if not clusters:
        return EventsSummaryResult("", "heuristic", stats)

    # Sort: Warning before Normal, then by count desc.
    def sort_key(item):
        (ev_type, reason, kind), c = item
        return (0 if ev_type == "Warning" else 1, -c["count"])

    lines: List[str] = []
    for (ev_type, reason, kind), c in sorted(clusters.items(), key=sort_key):
        objects = sorted(c["objects"])
        obj_part = ""
        if objects:
            if len(objects) <= 3:
                obj_part = f" on {kind}/{','.join(objects)}"
            else:
                obj_part = f" on {kind} (×{len(objects)} objects)"
        last = f" [last_seen={c['last_seen']}]" if c["last_seen"] else ""
        lines.append(
            f"[{ev_type}] {reason} ×{c['count']}{obj_part}: "
            f"{c['latest_message'][:200]}{last}"
        )

    stats.clusters = len(clusters)
    stats.events_out = sum(c["count"] for c in clusters.values())
    return EventsSummaryResult("\n".join(lines), "heuristic", stats)
