"""Apply a located span edit and render diffs; Kustomize patch-append fallback."""
from __future__ import annotations

import difflib

import yaml

from gitops.locate import FieldChange, Span


def apply_span(raw_text: str, span: Span, new_value) -> str:
    return raw_text[:span.start] + str(new_value) + raw_text[span.end:]


def unified_diff(path: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}",
    ))


def _nest(field_path: tuple, value) -> dict:
    """Build the minimal nested dict for a strategic-merge patch.

    Only used for map-path scalars (replicas, resources.*). Container-targeting
    changes go through the source-line editor, not this fallback, so no
    named-list handling is needed here.
    """
    if not field_path:
        return value
    head, *rest = field_path
    return {head: _nest(tuple(rest), value)}


def make_kustomize_patch(change: FieldChange, overlay_path: str) -> dict[str, str]:
    body = _nest(change.field_path, change.new_value)
    patch = {
        "apiVersion": "apps/v1",
        "kind": change.kind,
        "metadata": {"name": change.name},
    }
    # field_path always starts at the document root (e.g. ("spec","replicas")),
    # so merge the nested body's top-level keys onto the patch.
    patch.update(body)
    patch_name = f"kubeastra-{change.name}-patch.yaml"
    patch_path = f"{overlay_path}/{patch_name}"
    kustomization = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "patches": [{"path": patch_name}],
    }
    return {
        patch_path: yaml.safe_dump(patch, sort_keys=False),
        f"{overlay_path}/kustomization.yaml": yaml.safe_dump(kustomization, sort_keys=False),
    }
