"""Load the rule catalogue (``mcp/data/resilience/rules.yaml``) and apply a
repo's overrides (``.kubeastra/resilience.yaml``: ``disable`` / ``severity``).

The catalogue sits next to the core the same way the upgrade maps do, so the
vendored CLI copy (``kubeastra/_vendor/services/resilience`` +
``_vendor/data/resilience``) finds it with the same relative path.
"""
from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

from .rules import RULES

# mcp/services/resilience/catalogue.py -> parents[2] == mcp/, then data/resilience.
_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "resilience"
_SEVERITIES = ("low", "medium", "high")


@lru_cache(maxsize=1)
def _load_default() -> dict[str, Any]:
    return yaml.safe_load((_DATA_DIR / "rules.yaml").read_text())


def load_rules(path: Optional[str | Path] = None) -> dict[str, Any]:
    """The catalogue as a fresh dict (callers may mutate it). Validated: every
    rule function has an entry and vice versa, severities and weights are known."""
    cat = copy.deepcopy(yaml.safe_load(Path(path).read_text()) if path else _load_default())
    rules = cat.get("rules") or {}
    if set(rules) != set(RULES):
        raise ValueError(
            f"rules.yaml and rules.py disagree: only in yaml {sorted(set(rules) - set(RULES))}, "
            f"only in code {sorted(set(RULES) - set(rules))}"
        )
    for rid, meta in rules.items():
        if meta.get("severity") not in _SEVERITIES:
            raise ValueError(f"{rid}: severity must be one of {_SEVERITIES}")
        meta.setdefault("enabled", True)
    if set(cat.get("weights") or {}) != set(_SEVERITIES):
        raise ValueError(f"weights must define exactly {_SEVERITIES}")
    return cat


def apply_config(cat: dict[str, Any], config: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Apply a repo config ``{"disable": [ids], "severity": {id: sev}}``.
    Unknown ids or severities are an error — a typo must not silently do nothing."""
    if not config:
        return cat
    unknown_keys = set(config) - {"disable", "severity"}
    if unknown_keys:
        raise ValueError(f"unknown resilience config keys: {sorted(unknown_keys)}")
    rules = cat["rules"]
    for rid in config.get("disable") or []:
        if rid not in rules:
            raise ValueError(f"config disables unknown rule {rid!r}")
        rules[rid]["enabled"] = False
    for rid, sev in (config.get("severity") or {}).items():
        if rid not in rules:
            raise ValueError(f"config sets severity for unknown rule {rid!r}")
        if sev not in _SEVERITIES:
            raise ValueError(f"{rid}: severity must be one of {_SEVERITIES}, got {sev!r}")
        rules[rid]["severity"] = sev
    return cat
