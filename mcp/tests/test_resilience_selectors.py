"""Resilience Pilot Phase 0: label-selector semantics.

Every cross-object rule (PDB → pods, Service → pods) depends on these, so they
are pinned against the Kubernetes docs case by case, including the two
"empty" semantics that differ: a PDB's ``{}`` selects everything, a Service's
``{}`` selects nothing.
"""
import itertools
import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from services.resilience.selectors import label_selector_matches, map_selector_matches  # noqa: E402

POD = {"app": "web", "tier": "front", "track": "stable"}


@pytest.mark.parametrize(
    "selector,expected",
    [
        (None, False),
        ({}, True),
        ({"matchLabels": {"app": "web"}}, True),
        ({"matchLabels": {"app": "web", "tier": "front"}}, True),
        ({"matchLabels": {"app": "api"}}, False),
        ({"matchLabels": {"missing": "x"}}, False),
        ({"matchExpressions": [{"key": "tier", "operator": "In", "values": ["front", "back"]}]}, True),
        ({"matchExpressions": [{"key": "tier", "operator": "In", "values": ["back"]}]}, False),
        ({"matchExpressions": [{"key": "missing", "operator": "In", "values": ["x"]}]}, False),
        ({"matchExpressions": [{"key": "track", "operator": "NotIn", "values": ["canary"]}]}, True),
        ({"matchExpressions": [{"key": "track", "operator": "NotIn", "values": ["stable"]}]}, False),
        ({"matchExpressions": [{"key": "missing", "operator": "NotIn", "values": ["x"]}]}, True),
        ({"matchExpressions": [{"key": "app", "operator": "Exists"}]}, True),
        ({"matchExpressions": [{"key": "missing", "operator": "Exists"}]}, False),
        ({"matchExpressions": [{"key": "missing", "operator": "DoesNotExist"}]}, True),
        ({"matchExpressions": [{"key": "app", "operator": "DoesNotExist"}]}, False),
        ({"matchExpressions": [{"key": "app", "operator": "Gt", "values": ["1"]}]}, False),  # not a LabelSelector operator
        ({"matchLabels": {"app": "web"}, "matchExpressions": [{"key": "track", "operator": "In", "values": ["canary"]}]}, False),
        ({"matchLabels": "app=web"}, False),
    ],
)
def test_label_selector(selector, expected):
    assert label_selector_matches(selector, POD) is expected


@pytest.mark.parametrize(
    "selector,expected",
    [(None, False), ({}, False), ({"app": "web"}, True), ({"app": "web", "tier": "front"}, True), ({"app": "api"}, False)],
)
def test_service_selector(selector, expected):
    assert map_selector_matches(selector, POD) is expected


def test_values_compare_as_strings():
    assert label_selector_matches({"matchLabels": {"version": 2}}, {"version": "2"})
    assert map_selector_matches({"version": "2"}, {"version": 2})


def test_matchlabels_equals_the_equivalent_in_expressions():
    """matchLabels k=v is defined as an In requirement with one value — check
    the two spellings agree over every subset of a label set."""
    keys = ["app", "tier", "track", "zone"]
    universe = {"app": "web", "tier": "front", "track": "stable", "zone": "a"}
    for r in range(len(keys) + 1):
        for subset in itertools.combinations(keys, r):
            sel_labels = {k: universe[k] for k in subset}
            as_expr = [{"key": k, "operator": "In", "values": [v]} for k, v in sel_labels.items()]
            for pod_keys in itertools.combinations(keys, 2):
                pod = {k: universe[k] for k in pod_keys}
                assert label_selector_matches({"matchLabels": sel_labels}, pod) == label_selector_matches({"matchExpressions": as_expr}, pod)
