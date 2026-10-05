"""Load + validate the Upgrade Pilot data maps.

Pure: ``yaml`` + stdlib only (no web, no DB, no LLM, no ``ui/``). These maps are
the deterministic knowledge the planner reasons over — the API-deprecation map
(guaranteed), the provider skew policy, and the advisory operator-compat hints.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

# mcp/services/upgrade/maps.py -> parents[2] == mcp/, then data/upgrades.
_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "upgrades"


def minor_tuple(version: str) -> tuple[int, int]:
    """Parse a Kubernetes version to ``(major, minor)``.

    Accepts "1.31", "1.31.4", "v1.31", "v1.31.4". Raises ValueError otherwise.
    """
    v = (version or "").strip().lstrip("vV")
    parts = v.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(f"not a Kubernetes version: {version!r}")
    return int(parts[0]), int(parts[1])


def minor_le(a: str, b: str) -> bool:
    """True when minor version ``a`` is <= minor version ``b`` (patch ignored)."""
    return minor_tuple(a) <= minor_tuple(b)


@dataclass(frozen=True)
class Deprecation:
    kind: str
    group: str
    version: str
    removed_in: str
    deprecated_in: str = ""
    replacement: Optional[str] = None

    @property
    def api_version(self) -> str:
        return f"{self.group}/{self.version}" if self.group else self.version

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.group, self.version, self.kind)


_REQUIRED_DEP_FIELDS = ("kind", "group", "version", "removed_in")
_ALLOWED_DEP_FIELDS = set(_REQUIRED_DEP_FIELDS) | {"deprecated_in", "replacement"}


def validate_api_deprecations(raw: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems (empty = valid)."""
    problems: list[str] = []
    entries = raw.get("deprecations")
    if not isinstance(entries, list) or not entries:
        return ["api_deprecations.yaml: 'deprecations' must be a non-empty list"]
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            problems.append(f"deprecations[{i}] is not a mapping")
            continue
        for f in _REQUIRED_DEP_FIELDS:
            if not e.get(f):
                problems.append(f"deprecations[{i}] ({e.get('kind', '?')}) missing '{f}'")
        unknown = set(e) - _ALLOWED_DEP_FIELDS
        if unknown:
            problems.append(f"deprecations[{i}] has unknown fields: {sorted(unknown)}")
        # `replacement` must be PRESENT (may be null) so "removed with no
        # replacement" is explicit, never an accidental omission.
        if "replacement" not in e:
            problems.append(
                f"deprecations[{i}] ({e.get('kind', '?')}) must set 'replacement' "
                f"(use null when the API is removed with no in-place replacement)"
            )
        for vf in ("removed_in", "deprecated_in"):
            if e.get(vf):
                try:
                    minor_tuple(str(e[vf]))
                except ValueError as exc:
                    problems.append(f"deprecations[{i}] {vf}: {exc}")
    return problems


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def load_maps(data_dir: Optional[Path] = None, *, strict: bool = False) -> dict[str, Any]:
    """Load all three maps. With ``strict=True`` raise on an invalid deprecation
    map; otherwise return what parsed (the planner degrades gracefully)."""
    d = Path(data_dir) if data_dir else _DATA_DIR
    api_raw = _load_yaml(d / "api_deprecations.yaml")
    if strict:
        problems = validate_api_deprecations(api_raw)
        if problems:
            raise ValueError("invalid api_deprecations.yaml:\n  " + "\n  ".join(problems))

    deprecations = [
        Deprecation(
            kind=e["kind"],
            group=e.get("group", "") or "",
            version=e["version"],
            removed_in=str(e["removed_in"]),
            deprecated_in=str(e.get("deprecated_in", "") or ""),
            replacement=e.get("replacement"),
        )
        for e in (api_raw.get("deprecations") or [])
        if isinstance(e, dict) and e.get("kind") and e.get("version") and e.get("removed_in")
    ]
    return {
        "deprecations": deprecations,
        "provider_eol": _load_yaml(d / "provider_eol.yaml"),
        "operator_compat": _load_yaml(d / "operator_compat.yaml"),
    }
