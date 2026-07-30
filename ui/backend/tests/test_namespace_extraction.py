"""Pulling a namespace out of ordinary English.

The old extraction was `namespace[:\\s]+(\\S+)|in\\s+([a-z0-9-]+)`, copy-pasted
to eleven call sites, and it failed three different ways:

  "list all pods in the production namespace"  -> "the"
  "get pods in namespace demo"                 -> "namespace"
  "show pods in imagepullbackoff"              -> "imagepullbackoff"

The second is the subtle one. Regex alternation takes the *leftmost* match,
so `in\\s+(...)` fires on "in namespace" before `namespace[:\\s]+(...)` is ever
reached. Each of these then queried a namespace that does not exist and
reported "no pods found" as though that were the answer — which is worse than
an error, because it looks like information.
"""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from routers.chat import _extract_namespace, _extract_namespace_or_all  # noqa: E402


# ── the three reported failures ───────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "list all pods in the production namespace",
        "show me pods in the production namespace",
        "what is running in the production namespace",
    ],
)
def test_the_article_is_not_the_namespace(message):
    assert _extract_namespace(message) == "production"


def test_the_word_namespace_is_not_the_namespace(message="get pods in namespace demo"):
    """Leftmost-match made `in\\s+(...)` win over the explicit form."""
    assert _extract_namespace(message) == "demo"


@pytest.mark.parametrize(
    "condition", ["crashloop", "crashloopbackoff", "imagepullbackoff", "oomkilled", "pending"]
)
def test_a_pod_condition_is_not_a_namespace(condition):
    assert _extract_namespace(f"show pods in {condition}") is None


# ── forms that must keep working ──────────────────────────────────────────


@pytest.mark.parametrize(
    "message,expected",
    [
        ("get pods -n demo", "demo"),
        ("kubectl get pods -n kube-system", "kube-system"),
        ("get pods --namespace=demo", "demo"),
        ("get pods --namespace kube-system", "kube-system"),
        ("namespace: staging", "staging"),
        ("pods in the kube-system namespace", "kube-system"),
        ("pods in kube-system namespace", "kube-system"),
        ("show pods in production", "production"),
        ("what is in the namespace kube-system", "kube-system"),
    ],
)
def test_recognised_forms(message, expected):
    assert _extract_namespace(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "list pods",
        "show me the pods",
        "pods in the cluster",
        "what is happening here",
        "",
    ],
)
def test_no_namespace_means_none_not_a_guess(message):
    """Returning a wrong namespace is worse than returning nothing: the
    caller can auto-discover, but it cannot know it was told a lie."""
    assert _extract_namespace(message) is None


def test_the_default_is_used_only_when_nothing_was_named():
    assert _extract_namespace("list pods", "default") == "default"
    assert _extract_namespace("list pods in demo", "default") == "demo"


# ── the "all namespaces" variant ──────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "list pods in all namespaces",
        "show pods across the cluster",
        "pods in every namespace",
        "list all pods",
    ],
)
def test_cluster_wide_phrasings_mean_all(message):
    assert _extract_namespace_or_all(message) == "*"


def test_a_named_namespace_still_wins_over_the_all_default():
    assert _extract_namespace_or_all("list pods in demo") == "demo"
    assert _extract_namespace_or_all("list all pods in the demo namespace") == "demo"


def test_hyphens_and_dots_survive():
    assert _extract_namespace("pods in the kube-node-lease namespace") == "kube-node-lease"
    assert _extract_namespace("pods in my.team.ns namespace") == "my.team.ns"
