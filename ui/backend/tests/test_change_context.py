"""`get_recent_changes` — "what deployed just before this broke?"

A rollout minutes before an alert is the most common cause of a sudden
failure, and Kubernetes already records it: every change to a Deployment's pod
template creates a new ReplicaSet, and its creationTimestamp is when that
rollout started. No CI integration required.

What is pinned here is mostly parsing, because that is where this goes wrong
quietly. A timestamp that fails to parse, an ownerReference that is not a
workload, or a diff computed against the wrong revision all produce a
confident answer that happens to be false — and "nothing changed recently" is
a conclusion an operator will act on.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[3] / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from k8s import wrappers  # noqa: E402


def _stamp(minutes_ago: int) -> str:
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    # Kubernetes emits a literal Z, which is the format the parser must accept.
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _rs(
    name: str,
    owner: str,
    minutes_ago: int,
    image: str = "app:v1",
    revision: str = "1",
    kind: str = "Deployment",
    change_cause: str | None = None,
    ready: int = 1,
    replicas: int = 1,
):
    annotations = {"deployment.kubernetes.io/revision": revision}
    if change_cause:
        annotations["kubernetes.io/change-cause"] = change_cause
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": _stamp(minutes_ago),
            "annotations": annotations,
            "ownerReferences": [{"kind": kind, "name": owner}],
        },
        "spec": {
            "replicas": replicas,
            "template": {"spec": {"containers": [{"name": "app", "image": image}]}},
        },
        "status": {"readyReplicas": ready},
    }


class FakeRunner:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def run_json(self, args, namespace=None):
        self.calls.append(list(args))
        return {"items": self.items}


@pytest.fixture
def runner(monkeypatch):
    def install(items):
        fake = FakeRunner(items)
        monkeypatch.setattr(wrappers, "get_runner", lambda: fake)
        return fake

    return install


# ── the core question ─────────────────────────────────────────────────────


def test_a_recent_rollout_is_reported_with_its_image_change(runner):
    runner([
        _rs("api-old", "api", minutes_ago=300, image="app:v1", revision="1"),
        _rs("api-new", "api", minutes_ago=8, image="app:v2", revision="2"),
    ])

    result = wrappers.get_recent_changes("payments", within_minutes=60)

    assert len(result["changes"]) == 1
    change = result["changes"][0]
    assert change["workload"] == "api"
    assert change["revision"] == "2"
    assert change["image_changes"] == [
        {"container": "app", "from": "app:v1", "to": "app:v2"}
    ]


def test_nothing_recent_is_a_real_answer_not_an_empty_failure(runner):
    """"Nothing deployed recently" rules out the most likely cause.

    It has to be distinguishable from a failed lookup, which is why the
    payload still carries the window and namespace.
    """
    runner([_rs("api-old", "api", minutes_ago=600)])

    result = wrappers.get_recent_changes("payments", within_minutes=60)

    assert result["changes"] == []
    assert result["window_minutes"] == 60
    assert "error" not in result


def test_a_rollout_that_changed_no_image_still_reports(runner):
    """Env, resources and probe changes create a ReplicaSet too.

    Reporting the rollout with an empty diff redirects the investigation;
    hiding it would suggest nothing happened.
    """
    runner([
        _rs("api-1", "api", minutes_ago=200, image="app:v1", revision="1"),
        _rs("api-2", "api", minutes_ago=5, image="app:v1", revision="2"),
    ])

    change = wrappers.get_recent_changes("payments")["changes"][0]

    assert change["image_changes"] == []
    assert change["revision"] == "2"


def test_the_diff_is_against_the_previous_revision_not_the_oldest(runner):
    """Three revisions in the window: v2 -> v3 is the change that just landed.

    Diffing against the oldest would report `v1 -> v3` and point the operator
    at a change that shipped hours earlier.
    """
    runner([
        _rs("api-1", "api", minutes_ago=180, image="app:v1", revision="1"),
        _rs("api-2", "api", minutes_ago=90, image="app:v2", revision="2"),
        _rs("api-3", "api", minutes_ago=4, image="app:v3", revision="3"),
    ])

    change = wrappers.get_recent_changes("payments", within_minutes=240)["changes"][0]

    assert change["image_changes"] == [
        {"container": "app", "from": "app:v2", "to": "app:v3"}
    ]


def test_change_cause_is_surfaced_when_recorded(runner):
    runner([_rs("api-1", "api", minutes_ago=3, change_cause="kubectl set image ...")])

    change = wrappers.get_recent_changes("payments")["changes"][0]

    assert change["change_cause"] == "kubectl set image ..."


def test_a_missing_change_cause_is_not_an_error(runner):
    """Nobody records it unless they used --record or a CI system wrote it."""
    runner([_rs("api-1", "api", minutes_ago=3)])

    assert wrappers.get_recent_changes("payments")["changes"][0]["change_cause"] is None


# ── ordering and filtering ────────────────────────────────────────────────


def test_changes_are_newest_first(runner):
    """The most recent change is the likeliest cause, so it reads first."""
    runner([
        _rs("api-1", "api", minutes_ago=30),
        _rs("worker-1", "worker", minutes_ago=2),
        _rs("cache-1", "cache", minutes_ago=15),
    ])

    names = [c["workload"] for c in wrappers.get_recent_changes("payments")["changes"]]

    assert names == ["worker", "cache", "api"]


def test_a_workload_filter_narrows_to_one(runner):
    runner([
        _rs("api-1", "api", minutes_ago=5),
        _rs("worker-1", "worker", minutes_ago=5),
    ])

    result = wrappers.get_recent_changes("payments", workload_name="api")

    assert [c["workload"] for c in result["changes"]] == ["api"]


def test_statefulsets_and_daemonsets_count_as_workloads(runner):
    runner([
        _rs("s-1", "db", minutes_ago=5, kind="StatefulSet"),
        _rs("d-1", "agent", minutes_ago=5, kind="DaemonSet"),
    ])

    kinds = {c["kind"] for c in wrappers.get_recent_changes("payments")["changes"]}

    assert kinds == {"StatefulSet", "DaemonSet"}


def test_an_orphaned_replicaset_is_skipped(runner):
    """No workload owner means nothing an operator can act on."""
    orphan = _rs("stray", "gone", minutes_ago=2)
    orphan["metadata"]["ownerReferences"] = []
    runner([orphan])

    assert wrappers.get_recent_changes("payments")["changes"] == []


def test_a_replicaset_owned_by_something_unexpected_is_skipped(runner):
    runner([_rs("odd-1", "thing", minutes_ago=2, kind="CronJob")])

    assert wrappers.get_recent_changes("payments")["changes"] == []


# ── parsing and bounds ────────────────────────────────────────────────────


def test_an_unparseable_timestamp_does_not_abort_the_answer(runner):
    """One malformed object must not cost the other workloads' answers."""
    broken = _rs("broken-1", "broken", minutes_ago=2)
    broken["metadata"]["creationTimestamp"] = "not-a-date"
    runner([broken, _rs("api-1", "api", minutes_ago=2)])

    result = wrappers.get_recent_changes("payments")

    assert [c["workload"] for c in result["changes"]] == ["api"]


def test_the_window_is_bounded(runner):
    """An unbounded window turns this into a full-history scan."""
    runner([])

    huge = wrappers.get_recent_changes("payments", within_minutes=999_999)
    tiny = wrappers.get_recent_changes("payments", within_minutes=0)

    assert huge["window_minutes"] == 60 * 24 * 7
    assert tiny["window_minutes"] == 1


def test_a_kubectl_failure_is_reported_not_raised(runner, monkeypatch):
    """This runs inside investigations; a raise would abort the whole thing
    over a question that is only ever supplementary."""
    from k8s.kubectl_runner import KubectlError

    class Failing:
        def run_json(self, args, namespace=None):
            raise KubectlError(
                "forbidden: cannot list replicasets", 1, "Error from server (Forbidden)"
            )

    monkeypatch.setattr(wrappers, "get_runner", lambda: Failing())

    result = wrappers.get_recent_changes("payments")

    assert result["changes"] == []
    assert "forbidden" in result["error"]


def test_the_namespace_is_validated(runner):
    runner([])

    with pytest.raises(Exception):
        wrappers.get_recent_changes("../../etc/passwd")


# ── registry wiring ───────────────────────────────────────────────────────


def test_the_tool_is_registered_and_read_only():
    import tool_registry

    definition = tool_registry.TOOLS["get_recent_changes"]

    assert definition.write_op is False
    assert {"chat", "react", "mcp"} <= set(definition.surfaces)


def test_the_description_tells_the_model_when_to_reach_for_it():
    """The agent picks tools from these descriptions. One that does not say
    *when* to use it is a tool the model never calls."""
    import tool_registry

    description = tool_registry.TOOLS["get_recent_changes"].description.lower()

    assert "first" in description
    assert "nothing changed" in description


def test_dispatch_reaches_the_wrapper(monkeypatch):
    import tool_registry

    seen = {}

    def fake(namespace, within_minutes=60, workload_name=None):
        seen.update(
            namespace=namespace,
            within_minutes=within_minutes,
            workload_name=workload_name,
        )
        return {"changes": []}

    monkeypatch.setattr(wrappers, "get_recent_changes", fake)

    tool_registry.dispatch(
        "get_recent_changes",
        {"namespace": "payments", "within_minutes": 15, "workload_name": "api"},
        tool_registry.DispatchContext(surface="react"),
    )

    assert seen == {
        "namespace": "payments",
        "within_minutes": 15,
        "workload_name": "api",
    }
