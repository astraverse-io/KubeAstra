"""Kubernetes label-selector matching, as the API server does it.

Cross-object rules are only as right as these links (PDB → pods, Service →
pods), so the semantics follow the Kubernetes docs exactly:

- ``LabelSelector`` (PDB ``spec.selector``): ``None`` selects **nothing**;
  ``{}`` selects **everything** in the namespace (policy/v1); otherwise every
  ``matchLabels`` pair AND every ``matchExpressions`` requirement must hold.
- Service ``spec.selector`` is a plain map: empty or absent selects **nothing**
  (a Service without a selector has manually managed endpoints).

An unknown ``matchExpressions`` operator matches nothing — the API server would
have rejected it, so the manifest can't be the one that actually runs.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional


def _requirement_holds(req: Any, labels: Mapping[str, str]) -> bool:
    if not isinstance(req, dict):
        return False
    key = req.get("key")
    op = req.get("operator")
    values = [str(v) for v in (req.get("values") or [])]
    if not isinstance(key, str):
        return False
    if op == "In":
        return key in labels and labels[key] in values
    if op == "NotIn":
        return key not in labels or labels[key] not in values
    if op == "Exists":
        return key in labels
    if op == "DoesNotExist":
        return key not in labels
    return False


def label_selector_matches(selector: Optional[Mapping[str, Any]], labels: Optional[Mapping[str, Any]]) -> bool:
    """``LabelSelector`` semantics (``matchLabels`` + ``matchExpressions``)."""
    if selector is None or not isinstance(selector, Mapping):
        return False
    labels = {str(k): str(v) for k, v in (labels or {}).items()}
    match_labels = selector.get("matchLabels") or {}
    expressions = selector.get("matchExpressions") or []
    if not isinstance(match_labels, Mapping) or not isinstance(expressions, list):
        return False
    for k, v in match_labels.items():
        if labels.get(str(k)) != str(v):
            return False
    return all(_requirement_holds(r, labels) for r in expressions)


def map_selector_matches(selector: Optional[Mapping[str, Any]], labels: Optional[Mapping[str, Any]]) -> bool:
    """Service ``spec.selector`` semantics: a non-empty equality map."""
    if not selector or not isinstance(selector, Mapping):
        return False
    labels = {str(k): str(v) for k, v in (labels or {}).items()}
    return all(labels.get(str(k)) == str(v) for k, v in selector.items())
