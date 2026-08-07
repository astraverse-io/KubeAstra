"""The chart must not grant more than the deployment asked for.

The ClusterRole used to include `delete pods` and `create pods/exec`
unconditionally, with a comment explaining that the application setting blocked
them anyway. That is a description of the application, not of the cluster: the
ServiceAccount could delete any pod and exec into any container, in every
namespace, whether or not the feature was on.

An application-level flag is not an access control. It is precisely the thing
that an attacker who has reached the application does not have to satisfy.

These tests render the chart and read the result, rather than trusting the
template, because the failure mode is a conditional that silently does not
apply.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[3]
CHART = REPO_ROOT / "helm" / "kubeastra"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is not installed"
)


def _cluster_role(*settings: str) -> dict:
    command = ["helm", "template", "t", str(CHART)]
    for setting in settings:
        command += ["--set", setting]
    rendered = subprocess.run(
        command, capture_output=True, text=True, check=True
    ).stdout

    for document in yaml.safe_load_all(rendered):
        if document and document.get("kind") == "ClusterRole":
            return document
    raise AssertionError("the chart rendered no ClusterRole")


def _verbs_for(role: dict, resource: str) -> set[str]:
    verbs: set[str] = set()
    for rule in role.get("rules", []):
        if resource in rule.get("resources", []):
            verbs |= set(rule.get("verbs", []))
    return verbs


RECOVERY_ON = "backend.config.enableRecoveryOperations=true"
EXEC_ON = "rbac.allowPodExec=true"


def test_by_default_the_role_is_read_only():
    """The state every deployment starts in."""
    role = _cluster_role()

    for rule in role["rules"]:
        assert set(rule["verbs"]) <= {"get", "list", "watch"}, (
            f"default install grants {rule['verbs']} on {rule.get('resources')}"
        )


def test_write_verbs_need_recovery_operations_turned_on():
    assert _verbs_for(_cluster_role(), "deployments") == {"get", "list", "watch"}

    assert "patch" in _verbs_for(_cluster_role(RECOVERY_ON), "deployments")


def test_exec_is_never_granted_by_default():
    """`create` on pods/exec is arbitrary command execution in any container in
    any namespace — the broadest thing this chart can hand out."""
    assert _verbs_for(_cluster_role(), "pods/exec") == set()


def test_enabling_recovery_does_not_also_grant_exec():
    """The regression this file exists for. Nothing in remediation needs exec;
    turning on a rollout restart must not also hand over a shell."""
    assert _verbs_for(_cluster_role(RECOVERY_ON), "pods/exec") == set()


def test_exec_can_be_granted_deliberately():
    assert _verbs_for(_cluster_role(RECOVERY_ON, EXEC_ON), "pods/exec") == {"create"}


def test_asking_for_exec_alone_grants_nothing():
    """Both switches are required. One of them being flipped by somebody who
    did not read the other is the likely accident."""
    assert _verbs_for(_cluster_role(EXEC_ON), "pods/exec") == set()


def test_no_wildcard_is_ever_granted():
    """A `*` verb or resource would make every other assertion here
    meaningless."""
    for settings in ((), (RECOVERY_ON,), (RECOVERY_ON, EXEC_ON)):
        role = _cluster_role(*settings)
        for rule in role["rules"]:
            assert "*" not in rule.get("verbs", [])
            assert "*" not in rule.get("resources", [])
            assert "*" not in rule.get("apiGroups", [])


def test_secrets_are_never_readable():
    """Not required by any tool, and a ClusterRole that can read secrets
    cluster-wide is a credential store."""
    for settings in ((), (RECOVERY_ON,), (RECOVERY_ON, EXEC_ON)):
        assert _verbs_for(_cluster_role(*settings), "secrets") == set()
