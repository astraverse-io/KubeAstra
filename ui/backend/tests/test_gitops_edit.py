from __future__ import annotations
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import yaml  # noqa: E402
from gitops.locate import find_span, FieldChange  # noqa: E402
from gitops.edit import apply_span, unified_diff, make_kustomize_patch  # noqa: E402

DOC = """spec:
  replicas: 3            # keep in sync with HPA min
"""


def test_apply_span_changes_only_the_value():
    span = find_span(DOC, 0, ("spec", "replicas"))
    out = apply_span(DOC, span, 5)
    assert "replicas: 5" in out
    assert "keep in sync with HPA min" in out          # comment preserved
    assert yaml.safe_load(out)["spec"]["replicas"] == 5


def test_unquoted_scalar_stays_unquoted():
    span = find_span(DOC, 0, ("spec", "replicas"))
    out = apply_span(DOC, span, 5)
    assert "replicas: 5" in out and 'replicas: "5"' not in out


def test_double_quoted_scalar_keeps_its_quotes():
    # A quoted numeric-looking value must stay quoted, or YAML would coerce it
    # to a different type and the diff would be larger than the change.
    doc = 'spec:\n  containers:\n    - name: api\n      env:\n        - name: PORT\n          value: "3550"\n'
    span = find_span(doc, 0, ("spec", "containers", "api", "env", "PORT", "value"))
    out = apply_span(doc, span, "8080")
    assert 'value: "8080"' in out                       # quotes preserved
    val = yaml.safe_load(out)["spec"]["containers"][0]["env"][0]["value"]
    assert val == "8080" and isinstance(val, str)       # still a string


def test_single_quoted_scalar_keeps_single_quotes():
    doc = "spec:\n  replicas: '3'\n"
    span = find_span(doc, 0, ("spec", "replicas"))
    out = apply_span(doc, span, "5")
    assert "replicas: '5'" in out


def test_diff_is_one_line():
    span = find_span(DOC, 0, ("spec", "replicas"))
    out = apply_span(DOC, span, 5)
    diff = unified_diff("overlays/prod/api.yaml", DOC, out)
    added = [l for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
    removed = [l for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
    assert len(added) == 1 and len(removed) == 1


def test_kustomize_patch_targets_and_references():
    change = FieldChange(kind="Deployment", name="api-gateway", namespace="prod",
                         field_path=("spec", "replicas"), new_value=5, reason="load")
    files = make_kustomize_patch(change, "overlays/prod")
    # a patch file and the kustomization that includes it
    patch_path = next(p for p in files if p != "overlays/prod/kustomization.yaml")
    patch = yaml.safe_load(files[patch_path])
    assert patch["kind"] == "Deployment"
    assert patch["metadata"]["name"] == "api-gateway"
    assert patch["spec"]["replicas"] == 5
    kust = yaml.safe_load(files["overlays/prod/kustomization.yaml"])
    assert any(patch_path.endswith(Path(p).name) for p in
               (entry if isinstance(entry, str) else entry.get("path", "")
                for entry in kust.get("patches", []))) \
        or "patchesStrategicMerge" in kust
