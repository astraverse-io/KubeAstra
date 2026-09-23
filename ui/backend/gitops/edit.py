"""Apply a located span edit and render diffs; Kustomize patch-append fallback."""
from __future__ import annotations

import difflib

import yaml

from gitops.locate import FieldChange, Span


def _requote(original: str, new_value) -> str:
    """Render `new_value` in the same quoting style as the scalar it replaces.

    The located span covers the scalar exactly as written, including any
    surrounding quotes. Writing the replacement bare would strip those quotes —
    changing `value: "3550"` into `value: 3550` — which is both a needless diff
    and, for a value YAML would otherwise coerce to a number/bool, a type
    change. So a double- or single-quoted original keeps its quotes (with the
    minimal YAML escaping for that style); an unquoted original stays bare.
    """
    s = str(new_value)
    if len(original) >= 2 and original[0] == original[-1] and original[0] in ('"', "'"):
        if original[0] == '"':
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return "'" + s.replace("'", "''") + "'"
    return s


def apply_span(raw_text: str, span: Span, new_value) -> str:
    original = raw_text[span.start:span.end]
    return raw_text[:span.start] + _requote(original, new_value) + raw_text[span.end:]


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
