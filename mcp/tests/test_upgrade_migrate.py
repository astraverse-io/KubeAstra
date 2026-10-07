"""Phase 5: the deny-by-default migration transform guard (D4).

migrate_api_version may change only the sanctioned transform (apiVersion swap +
any map-specified field renames) and nothing else; plan_migration proves it or
refuses. Pure + keyless. See PILOTS_PLAN.md §Phase 5.
"""
import copy
import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from services.upgrade import build_migrated_object, diff_paths, plan_migration  # noqa: E402

LIVE = {
    "apiVersion": "rbac.authorization.k8s.io/v1beta1",
    "kind": "Role",
    "metadata": {"name": "r", "namespace": "ns"},
    "rules": [{"verbs": ["get"]}],
}


def test_pure_apiversion_swap_is_sanctioned():
    migrated, violations = plan_migration(
        LIVE,
        from_api_version="rbac.authorization.k8s.io/v1beta1",
        to_api_version="rbac.authorization.k8s.io/v1",
    )
    assert violations == []
    assert migrated["apiVersion"] == "rbac.authorization.k8s.io/v1"
    assert migrated["rules"] == LIVE["rules"]  # nothing else changed
    assert diff_paths(LIVE, migrated) == ["apiVersion"]


def test_refuses_when_live_apiversion_mismatches():
    # Refuse to migrate an object that isn't what the plan assessed.
    migrated, violations = plan_migration(
        LIVE,
        from_api_version="rbac.authorization.k8s.io/v1",  # live is v1beta1
        to_api_version="rbac.authorization.k8s.io/v1",
    )
    assert migrated is None
    assert any("apiVersion" in v for v in violations)


def test_field_rename_is_sanctioned():
    live = {"apiVersion": "x/v1beta1", "kind": "Foo", "spec": {"old": {"a": 1}}}
    migrated, violations = plan_migration(
        live, from_api_version="x/v1beta1", to_api_version="x/v1",
        field_renames={"spec.old": "spec.new"},
    )
    assert violations == []
    assert migrated["spec"]["new"] == {"a": 1}
    assert "old" not in migrated["spec"]


def test_refuses_empty_object():
    migrated, violations = plan_migration({}, from_api_version="a", to_api_version="b")
    assert migrated is None and violations


def test_build_migrated_object_does_not_mutate_input():
    original = copy.deepcopy(LIVE)
    build_migrated_object(LIVE, to_api_version="x/v1")
    assert LIVE == original  # operates on a deep copy


def test_diff_paths_detects_an_unsanctioned_change():
    a = {"apiVersion": "x/v1beta1", "spec": {"replicas": 1}}
    b = {"apiVersion": "x/v1", "spec": {"replicas": 2}}
    assert diff_paths(a, b) == ["apiVersion", "spec.replicas"]
