"""Phase 3.0 — Proactive cluster triage.

Runs a fast read-only scan of the user's currently-selected cluster
context and renders a one-screen markdown greeting that the chat router
prepends to the SSE stream when a session starts.

Scope (v1):
  * Reactive only — runs on the first message of a session, NOT in the
    background. Phase 3.1 will add the always-on polling watcher.
  * Read-only — uses ``get_pods`` and ``get_events`` from k8s.wrappers;
    no destructive operations, no LLM call.
  * Stateless — no dedup across sessions. Each connect re-scans.

Why this matters:
  Users land in the chat already in trouble — they wouldn't be opening
  the assistant otherwise. Surfacing the most obvious issues up front
  saves them typing the first question and lets them say "yes, the
  crashloop" instead of "what's wrong with my cluster."
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Max items per category in the greeting (prevents a 50-pod CrashLoop
# event from rendering a wall of text). Anything over this gets a
# "+N more" footer line.
_MAX_PER_CATEGORY = 5


def cluster_overview(
    *,
    namespace: str = "*",
    event_lookback_minutes: int = 10,
) -> Dict[str, Any]:
    """Fast read-only scan of the active cluster context.

    Returns a structured dict. ``anything_to_report`` is True iff any
    category has at least one entry — used by the caller to decide
    whether to emit the greeting or a terse "all clear" message.

    Calls the same kubectl wrappers the ReAct loop uses, so the result
    is consistent with what the assistant would surface in deeper
    investigation. Errors in any individual scan don't abort the whole
    overview — they're logged and the category is reported as empty.
    """
    started_at = time.time()

    overview: Dict[str, Any] = {
        "scanned_at": started_at,
        "namespace_filter": namespace,
        "crashlooping": [],
        "pending": [],
        "warning_events": [],
        "errors": [],          # per-category exceptions, never raised
    }

    # ── CrashLoopBackOff pods ───────────────────────────────────────────
    try:
        from k8s.wrappers import get_pods
        crash = get_pods(namespace=namespace, status_filter="CrashLoopBackOff")
        items = _extract_pods(crash)
        overview["crashlooping"] = items[:_MAX_PER_CATEGORY]
        overview["crashlooping_total"] = len(items)
    except Exception as exc:
        logger.warning("triage: get_pods(CrashLoopBackOff) failed: %s", exc)
        overview["errors"].append(f"crashlooping: {exc}")

    # ── Pending pods (stuck scheduling) ─────────────────────────────────
    try:
        from k8s.wrappers import get_pods
        pending = get_pods(namespace=namespace, status_filter="Pending")
        items = _extract_pods(pending)
        # Filter out pods that are *just* starting — a few seconds in
        # Pending is normal. Heuristic: if we have age info, drop those
        # under 60s.
        items = [p for p in items if _pending_long_enough(p)]
        overview["pending"] = items[:_MAX_PER_CATEGORY]
        overview["pending_total"] = len(items)
    except Exception as exc:
        logger.warning("triage: get_pods(Pending) failed: %s", exc)
        overview["errors"].append(f"pending: {exc}")

    # ── Warning events in the last N minutes ────────────────────────────
    try:
        from k8s.wrappers import get_events
        events = get_events(namespace=namespace, field_selector="type=Warning")
        recent = _filter_recent_warnings(
            events, lookback_minutes=event_lookback_minutes,
        )
        overview["warning_events"] = recent[:_MAX_PER_CATEGORY]
        overview["warning_events_total"] = len(recent)
    except Exception as exc:
        logger.warning("triage: get_events(Warning) failed: %s", exc)
        overview["errors"].append(f"warning_events: {exc}")

    # ── Anything to report? ─────────────────────────────────────────────
    overview["anything_to_report"] = bool(
        overview["crashlooping"]
        or overview["pending"]
        or overview["warning_events"]
    )
    overview["duration_ms"] = int((time.time() - started_at) * 1000)

    return overview


def render_greeting(
    overview: Dict[str, Any],
    *,
    cluster_label: Optional[str] = None,
) -> str:
    """Convert a cluster_overview() dict into the user-facing markdown.

    ``cluster_label`` is the human-readable context name to display
    (e.g. ``"prod-us-east"``). When None, the greeting omits the
    cluster name.

    Returns a markdown string ready to be emitted as an assistant
    message. Always ends with a soft prompt so the user knows the
    assistant is awake and waiting.
    """
    cluster_str = f" of `{cluster_label}`" if cluster_label else ""

    if not overview.get("anything_to_report"):
        return (
            f"👋 Quick scan{cluster_str}: **✅ Nothing flagged.**\n"
            "What can I help you with?"
        )

    lines: list[str] = [f"👋 Quick scan{cluster_str} before we start:"]

    crash = overview.get("crashlooping") or []
    crash_total = overview.get("crashlooping_total", len(crash))
    if crash:
        names = ", ".join(_format_pod_ref(p) for p in crash)
        suffix = f" (+{crash_total - len(crash)} more)" if crash_total > len(crash) else ""
        lines.append(f"- **{crash_total} pod{_s(crash_total)} CrashLooping**: {names}{suffix}")

    pending = overview.get("pending") or []
    pending_total = overview.get("pending_total", len(pending))
    if pending:
        names = ", ".join(_format_pod_ref(p) for p in pending)
        suffix = f" (+{pending_total - len(pending)} more)" if pending_total > len(pending) else ""
        lines.append(f"- **{pending_total} pending pod{_s(pending_total)}**: {names}{suffix}")

    warns = overview.get("warning_events") or []
    warn_total = overview.get("warning_events_total", len(warns))
    if warns:
        # Dedupe by reason for the user-visible summary
        reasons: Dict[str, int] = {}
        for e in warns:
            r = e.get("reason") or "Warning"
            reasons[r] = reasons.get(r, 0) + 1
        bits = [f"{count}× {reason}" for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1])]
        lines.append(
            f"- **{warn_total} warning event{_s(warn_total)}** in last 10 min: {', '.join(bits)}"
        )

    # Soft prompt — phrased to make a 'yes' useful as a follow-up
    primary_target = _primary_focus(overview)
    if primary_target:
        lines.append("")
        lines.append(f"Want me to investigate **{primary_target}** first?")
    else:
        lines.append("")
        lines.append("What would you like to look into?")

    return "\n".join(lines)


# ── Internal helpers ────────────────────────────────────────────────────

def _extract_pods(raw: Any) -> List[Dict[str, Any]]:
    """get_pods returns a dict whose shape varies a bit by status_filter.
    Normalize to a flat list of {namespace, name, status, age_seconds?, ...}."""
    if not isinstance(raw, dict):
        return []
    # Common shapes: {"pods": [...]} or {"items": [...]} or {"by_status": {"CrashLoop": [...]}}
    for key in ("pods", "items"):
        v = raw.get(key)
        if isinstance(v, list):
            return [p for p in v if isinstance(p, dict)]
    if isinstance(raw.get("by_status"), dict):
        out: list[dict] = []
        for v in raw["by_status"].values():
            if isinstance(v, list):
                out.extend(p for p in v if isinstance(p, dict))
        return out
    return []


def _pending_long_enough(pod: Dict[str, Any], min_seconds: int = 60) -> bool:
    """True unless we can confidently say the pod is < min_seconds old.

    The k8s wrappers return pod age as a human string like ``"5d"``,
    ``"2h"``, ``"30s"`` (from kubectl table output). Parse the trailing
    unit and compare. On any failure default to True (defensive —
    better to over-report than silently drop a real issue).
    """
    age_str = pod.get("age") or pod.get("age_seconds")
    if isinstance(age_str, (int, float)):
        return age_str >= min_seconds
    if not isinstance(age_str, str):
        return True

    import re as _re
    m = _re.match(r"^(\d+)([smhd])$", age_str.strip())
    if not m:
        return True
    n, unit = int(m.group(1)), m.group(2)
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * n
    return seconds >= min_seconds


def _filter_recent_warnings(
    raw: Any, *, lookback_minutes: int,
) -> List[Dict[str, Any]]:
    """Normalize the events response and keep only Warning events from
    the last ``lookback_minutes``. Falls back to the full list if we
    can't parse timestamps."""
    items: List[Dict[str, Any]] = []
    if isinstance(raw, dict):
        for key in ("events", "items"):
            v = raw.get(key)
            if isinstance(v, list):
                items = [e for e in v if isinstance(e, dict)]
                break
    elif isinstance(raw, list):
        items = [e for e in raw if isinstance(e, dict)]

    # If no timestamps, return everything that's a Warning
    cutoff = time.time() - lookback_minutes * 60
    out: List[Dict[str, Any]] = []
    for e in items:
        if (e.get("type") or "").lower() not in ("warning", ""):
            continue
        ts = _event_timestamp(e)
        if ts is None or ts >= cutoff:
            out.append(e)
    return out


def _event_timestamp(event: Dict[str, Any]) -> Optional[float]:
    """Try a few fields to find a unix timestamp on an event dict.

    The MCP's k8s/parsers.py:parse_events normalizes event dicts with
    snake_case ``last_timestamp`` / ``first_timestamp`` fields, so those
    must come first. The camelCase originals are kept as a fallback in
    case this is ever called against a raw Kubernetes event instead of
    the parsed shape.
    """
    for key in (
        "last_timestamp", "first_timestamp",      # parsed shape (primary)
        "lastTimestamp", "eventTime", "firstTimestamp",  # raw K8s fallback
    ):
        raw = event.get(key)
        if not raw:
            continue
        if isinstance(raw, (int, float)):
            return float(raw)
        try:
            from datetime import datetime
            # Handle trailing 'Z' as UTC
            iso = raw.replace("Z", "+00:00") if isinstance(raw, str) else raw
            return datetime.fromisoformat(iso).timestamp()
        except Exception:
            continue
    return None


def _format_pod_ref(pod: Dict[str, Any]) -> str:
    """Render `namespace/name` or just `name` if namespace is missing."""
    ns = pod.get("namespace")
    name = pod.get("name") or pod.get("pod_name") or "?"
    return f"`{ns}/{name}`" if ns else f"`{name}`"


def _s(n: int) -> str:
    """English plural 's' for n != 1."""
    return "" if n == 1 else "s"


def _primary_focus(overview: Dict[str, Any]) -> Optional[str]:
    """Pick a human-readable thing to offer investigating first.
    Prefers CrashLoop (most actionable) then Pending then warning reasons."""
    crash = overview.get("crashlooping") or []
    if crash:
        first = crash[0]
        ns = first.get("namespace")
        name = first.get("name") or first.get("pod_name")
        if ns and name:
            return f"{ns}/{name} (CrashLoopBackOff)"
        return f"the CrashLooping pod{_s(len(crash))}"
    pending = overview.get("pending") or []
    if pending:
        return f"the pending pod{_s(len(pending))}"
    warns = overview.get("warning_events") or []
    if warns:
        reason = warns[0].get("reason") or "Warning"
        return f"the {reason} events"
    return None
