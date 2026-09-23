from __future__ import annotations
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from gitops.locate import find_span, Span  # noqa: E402

DOC = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api-gateway
spec:
  replicas: 3            # bumped during the Nov incident
  template:
    spec:
      containers:
        - name: sidecar
          image: envoy:1.29
        - name: api
          image: ghcr.io/acme/api:v1.4.2
          resources:
            limits:
              memory: 128Mi
"""


def test_scalar_span_is_exact_and_value_only():
    span = find_span(DOC, 0, ("spec", "replicas"))
    assert DOC[span.start:span.end] == "3"
    assert span.old_value == "3"


def test_container_resolves_by_name_not_position():
    span = find_span(DOC, 0, ("spec", "template", "spec", "containers", "api", "image"))
    assert DOC[span.start:span.end] == "ghcr.io/acme/api:v1.4.2"
    # the sidecar's image must NOT be what we matched
    assert "envoy" not in DOC[span.start:span.end]


def test_deeply_nested_scalar():
    span = find_span(DOC, 0,
        ("spec", "template", "spec", "containers", "api", "resources", "limits", "memory"))
    assert DOC[span.start:span.end] == "128Mi"


def test_missing_path_returns_none():
    assert find_span(DOC, 0, ("spec", "nonexistent")) is None
    assert find_span(DOC, 0, ("spec", "template", "spec", "containers", "ghost", "image")) is None


# ── anchors / aliases: fail-closed (a single span can't edit a shared value) ──

ANCHORED = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  replicas: &r 3
  minReplicas: *r
"""


def test_alias_target_refuses():
    # `minReplicas: *r` resolves to the anchor node on the `replicas` line;
    # editing its span would silently change the wrong line, so refuse.
    assert find_span(ANCHORED, 0, ("spec", "minReplicas")) is None


def test_anchor_target_refuses():
    # editing `&r 3` would also change every `*r`, so the anchor itself is
    # not span-editable either.
    assert find_span(ANCHORED, 0, ("spec", "replicas")) is None


MERGE = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec: &base
  replicas: 3
overlay:
  <<: *base
  replicas: 5
"""


def test_merge_key_aliased_parent_refuses():
    # `overlay` pulls in the `&base` mapping via `<<: *base`; the shared parent
    # makes its scalars non-span-editable.
    assert find_span(MERGE, 0, ("spec", "replicas")) is None


def test_unaliased_anchor_refuses():
    # even an unreferenced anchor is refused: the scalar's span begins at `&u`,
    # so a span edit would silently drop the anchor property. Fail-closed rather
    # than make that surprising, non-minimal change.
    doc = "kind: Deployment\nmetadata:\n  name: api\nspec:\n  replicas: &u 3\n"
    assert find_span(doc, 0, ("spec", "replicas")) is None


def test_alias_free_doc_is_unaffected():
    # regression guard: shared-node detection must not refuse ordinary manifests.
    assert find_span(DOC, 0, ("spec", "replicas")).old_value == "3"
