"""Which workload is an alert actually about.

Three alerts about one crashlooping pod — CrashLoopBackOff, OOMKilled, a failing
probe — are one problem. Investigating them separately costs three LLM runs and
produces three answers to the same question, none of which mention the others.

Grouping them needs a stable key, and the only thing that reliably identifies a
workload across its pods is its name. Pod names carry generated suffixes, so
that name has to be recovered:

    api-7d4f9b8c5-x2k9p   ->  api      (Deployment: replicaset hash + pod hash)
    db-0                  ->  db       (StatefulSet: ordinal)
    node-agent-4kx9m      ->  node-agent  (DaemonSet: pod hash)

Getting this wrong is quiet in both directions. Strip too much and unrelated
workloads merge into one incident — `api` and `api-canary` become the same
thing. Strip too little and every pod of one Deployment is its own incident,
which is the behaviour this exists to remove.

Category is deliberately NOT part of the key. Including it would put a
workload's CrashLoopBackOff and its OOMKilled into different incidents, which
is precisely the split this is meant to avoid.
"""

from __future__ import annotations

import re

# Labels an alert may carry that name the workload outright. Preferred over
# any inference from the pod name, because they are what the operator meant.
WORKLOAD_LABELS = (
    "workload",
    "deployment",
    "statefulset",
    "daemonset",
    "job_name",
    "job",
)

# Kubernetes derives a Deployment's ReplicaSet suffix from a hash rendered in
# an alphabet that omits vowels and easily-confused digits, then adds a
# five-character pod suffix from the same alphabet. Matching the real alphabet
# rather than [a-z0-9] keeps `api-release-candidate` from being mistaken for a
# hashed name.
_HASH_ALPHABET = "bcdfghjklmnpqrstvwxz2456789"
_HASH = f"[{_HASH_ALPHABET}]"

_DEPLOYMENT_POD = re.compile(rf"^(?P<workload>.+)-{_HASH}{{5,10}}-[a-z0-9]{{5}}$")
_STATEFULSET_POD = re.compile(r"^(?P<workload>.+)-\d+$")
_DAEMONSET_POD = re.compile(rf"^(?P<workload>.+)-{_HASH}{{5}}$")


def derive_workload(labels: dict[str, str]) -> str:
    """The workload an alert is about, or "" when it cannot be determined.

    An empty result means "do not correlate this". That is the safe answer: a
    wrong key merges unrelated problems into one incident and buries whichever
    arrived second.
    """
    for label in WORKLOAD_LABELS:
        value = str(labels.get(label, "")).strip()
        if value:
            return value

    pod = str(labels.get("pod", "")).strip()
    if not pod:
        # Some alerts are namespace-scoped rather than about any one workload.
        # They are not correlated, which is correct — there is nothing to group
        # them by that would not also group unrelated things.
        return ""

    # Ordered most specific first. A Deployment pod also matches the DaemonSet
    # shape on its trailing segment, so that check has to come first or every
    # Deployment pod would keep its replicaset hash and never group.
    for pattern in (_DEPLOYMENT_POD, _STATEFULSET_POD, _DAEMONSET_POD):
        match = pattern.match(pod)
        if match:
            return match.group("workload")

    # A bare pod name with no generated suffix — a static pod, or something
    # created by hand. It is its own workload.
    return pod


def correlation_key(labels: dict[str, str]) -> tuple[str, str]:
    """`(namespace, workload)`, or `("", "")` when the alert cannot be grouped.

    Namespace is part of the key because the same workload name in staging and
    production is two different things, and merging them would attach a staging
    incident to a production one.
    """
    workload = derive_workload(labels)
    if not workload:
        return ("", "")
    namespace = str(labels.get("namespace", "")).strip()
    if not namespace:
        return ("", "")
    return (namespace, workload)
