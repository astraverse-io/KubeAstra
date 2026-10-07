"""Sanctioned apiVersion migration + deny-by-default diff guard.

The ``migrate_api_version`` write action may change an object's ``apiVersion``
(and only the deterministic field renames a migration explicitly specifies) — and
*nothing else*. This module computes that transform and **proves** the diff is
within the sanctioned set, so the write wrapper can refuse any object whose
required change exceeds it. That refusal is what keeps the remediation spine's
deny-by-default contract intact for a structural edit.

Pure: stdlib only (no web, no DB, no LLM, no ``ui/``). Semantic schema
compatibility (e.g. a v1beta1→v1 Ingress whose body structure also changed) is
NOT this module's job — the planner routes such objects to a reviewed PR /
advisory instead of to auto-apply. This guard enforces the *mechanical*
guarantee: an auto-applied object differs from the live one only by the
sanctioned transform.
"""
from __future__ import annotations

import copy
from typing import Any, Optional

_MISSING = object()


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a nested dict/list into {dotted.path: leaf_value}."""
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def diff_paths(a: Any, b: Any) -> list[str]:
    """Leaf paths where ``a`` and ``b`` differ (added, removed, or changed)."""
    fa, fb = _flatten(a), _flatten(b)
    return sorted(k for k in set(fa) | set(fb) if fa.get(k, _MISSING) != fb.get(k, _MISSING))


def _get(obj: dict, path: str) -> Any:
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _set(obj: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = obj
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _del(obj: dict, path: str) -> None:
    parts = path.split(".")
    cur = obj
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return
        cur = cur[part]
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


def build_migrated_object(
    live: dict, *, to_api_version: str, field_renames: Optional[dict[str, str]] = None
) -> dict:
    """Return a copy of ``live`` with its apiVersion set to ``to_api_version`` and
    any ``field_renames`` ({src_path: dst_path}) moved. Dotted paths only."""
    out = copy.deepcopy(live)
    out["apiVersion"] = to_api_version
    for src, dst in (field_renames or {}).items():
        val = _get(out, src)
        if val is not _MISSING:
            _del(out, src)
            _set(out, dst, val)
    return out


def _sanctioned_prefixes(field_renames: Optional[dict[str, str]]) -> set[str]:
    allowed = {"apiVersion"}
    for src, dst in (field_renames or {}).items():
        allowed.add(src)
        allowed.add(dst)
    return allowed


def _is_allowed(path: str, allowed: set[str]) -> bool:
    # A diff path is allowed if it equals, or is nested under, a sanctioned path.
    return any(path == a or path.startswith(a + ".") or path.startswith(a + "[") for a in allowed)


def plan_migration(
    live: dict,
    *,
    from_api_version: str,
    to_api_version: str,
    field_renames: Optional[dict[str, str]] = None,
) -> tuple[Optional[dict], list[str]]:
    """Compute the migrated object, or refuse.

    Returns ``(migrated, [])`` when the transform is sanctioned, or
    ``(None, violations)`` when a precondition fails or the resulting diff exceeds
    the sanctioned set — the deny-by-default guard the write wrapper enforces.
    """
    violations: list[str] = []
    if not isinstance(live, dict) or not live:
        return None, ["live object is empty or not a mapping"]

    actual = live.get("apiVersion")
    if actual != from_api_version:
        violations.append(
            f"live apiVersion is {actual!r}, expected {from_api_version!r} — refusing to migrate "
            f"an object that is not what the plan assessed"
        )
        return None, violations

    migrated = build_migrated_object(live, to_api_version=to_api_version, field_renames=field_renames)
    allowed = _sanctioned_prefixes(field_renames)
    unexpected = [p for p in diff_paths(live, migrated) if not _is_allowed(p, allowed)]
    if unexpected:
        violations.append(
            "refusing: the migration would change fields beyond the sanctioned transform: "
            + ", ".join(unexpected)
        )
        return None, violations
    return migrated, []
