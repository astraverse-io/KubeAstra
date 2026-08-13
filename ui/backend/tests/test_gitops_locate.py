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
