"""Recovering the workload name from an alert.

Wrong in either direction and it is silent. Strip too much and `api` and
`api-canary` merge into one incident, so a canary failure hides inside the
stable deployment's. Strip too little and every pod of a Deployment is its own
incident — which is the fragmentation this exists to remove.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import alert_correlation as corr  # noqa: E402


# ── explicit labels win ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "label", ["workload", "deployment", "statefulset", "daemonset", "job_name"]
)
def test_an_explicit_workload_label_is_used_as_given(label: str):
    """These are what the operator meant. Never second-guess them by parsing
    the pod name instead."""
    assert corr.derive_workload({label: "payments-api", "pod": "x-abcde-12345"}) == (
        "payments-api"
    )


def test_an_empty_label_falls_through_to_the_pod_name():
    assert corr.derive_workload({"deployment": "  ", "pod": "api-7d4f9b-x2k9p"}) == "api"


# ── recovering it from a pod name ─────────────────────────────────────────


def test_a_deployment_pod_loses_both_generated_suffixes():
    assert corr.derive_workload({"pod": "api-7d4f9b8c5-x2k9p"}) == "api"


def test_a_statefulset_pod_loses_its_ordinal():
    assert corr.derive_workload({"pod": "postgres-0"}) == "postgres"
    assert corr.derive_workload({"pod": "postgres-11"}) == "postgres"


def test_a_daemonset_pod_loses_its_hash():
    assert corr.derive_workload({"pod": "node-exporter-4kx9m"}) == "node-exporter"


def test_two_pods_of_one_deployment_give_the_same_workload():
    """The whole point: these must correlate into one incident."""
    first = corr.derive_workload({"pod": "api-7d4f9b8c5-x2k9p"})
    second = corr.derive_workload({"pod": "api-7d4f9b8c5-qq7rt"})

    assert first == second == "api"


def test_pods_from_different_revisions_still_correlate():
    """A rollout replaces the ReplicaSet hash. Mid-rollout alerts from old and
    new pods are the same incident — usually the rollout itself."""
    old = corr.derive_workload({"pod": "api-7d4f9b8c5-x2k9p"})
    new = corr.derive_workload({"pod": "api-6b2c8d4f7-mm3wz"})

    assert old == new == "api"


def test_a_hyphenated_workload_name_survives():
    assert corr.derive_workload({"pod": "payments-api-7d4f9b8c5-x2k9p"}) == "payments-api"


def test_a_bare_pod_name_is_its_own_workload():
    """A static pod, or something created by hand — no generated suffix to
    strip."""
    assert corr.derive_workload({"pod": "etcd-master-1.example.com"}) != ""


# ── what must NOT be stripped ─────────────────────────────────────────────


def test_a_name_ending_in_a_word_is_not_mistaken_for_a_hash():
    """`candidate` is nine characters of lowercase — a naive [a-z0-9]{5,10}
    would eat it. Kubernetes hashes come from a vowel-free alphabet, so
    matching that alphabet keeps real words intact."""
    assert corr.derive_workload({"pod": "api-release-candidate"}) == (
        "api-release-candidate"
    )


def test_a_canary_is_not_merged_into_the_stable_deployment():
    """These are different workloads and often failing for different reasons.
    Merging them hides the canary failure inside the stable incident."""
    stable = corr.derive_workload({"pod": "api-7d4f9b8c5-x2k9p"})
    canary = corr.derive_workload({"pod": "api-canary-7d4f9b8c5-x2k9p"})

    assert stable != canary
    assert canary == "api-canary"


# ── the correlation key ───────────────────────────────────────────────────


def test_the_key_is_namespace_and_workload():
    assert corr.correlation_key(
        {"namespace": "prod", "pod": "api-7d4f9b8c5-x2k9p"}
    ) == ("prod", "api")


def test_the_same_workload_in_two_namespaces_does_not_correlate():
    """Staging and production are different problems. Merging them would
    attach a staging incident to a production one."""
    prod = corr.correlation_key({"namespace": "prod", "deployment": "api"})
    staging = corr.correlation_key({"namespace": "staging", "deployment": "api"})

    assert prod != staging


def test_the_alert_category_is_not_part_of_the_key():
    """Including it would split a workload's CrashLoopBackOff from its
    OOMKilled — the exact split this feature exists to remove."""
    crashloop = corr.correlation_key(
        {"namespace": "prod", "deployment": "api", "alertname": "CrashLoopBackOff"}
    )
    oom = corr.correlation_key(
        {"namespace": "prod", "deployment": "api", "alertname": "OOMKilled"}
    )

    assert crashloop == oom


def test_an_alert_with_no_workload_is_not_correlated():
    """A namespace-scoped alert has nothing to group it by that would not also
    group unrelated things. Empty means "leave it alone"."""
    assert corr.correlation_key({"namespace": "prod"}) == ("", "")


def test_an_alert_with_no_namespace_is_not_correlated():
    """Without a namespace the key is ambiguous across clusters and tenants,
    and a wrong merge is worse than no merge."""
    assert corr.correlation_key({"deployment": "api"}) == ("", "")
