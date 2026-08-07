"""Putting a routing decision into effect.

`cluster_routing` decides which cluster. This is the part that makes kubectl
actually go there, by installing an SSH runner on the contextvar every kubectl
call already reads.

Two properties matter more than the plumbing, and both are tested by breaking
them:

  * **Never fall back.** A target that cannot be reached must fail the
    investigation. Leaving the default runner installed is the original bug in
    disguise — an investigation that looks routed, runs somewhere else, and
    produces a confident answer about the wrong machine.
  * **Always close.** A live runner holds a socket, a paramiko transport thread
    and a session on the target's sshd. Leaking them exhausts the target's
    MaxSessions and this pod's file descriptors, and shows up as "SSH stopped
    working" long after the investigation that caused it.

Verified against fakes, not a real cluster. What that does and does not prove
is called out at the bottom.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import cluster_execution as execution  # noqa: E402
from k8s.kubectl_runner import get_runner  # noqa: E402


class FakeRunner:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def secret_dir(tmp_path):
    (tmp_path / "cluster-ssh").write_text("-----BEGIN PRIVATE KEY-----\nfake\n")
    return str(tmp_path)


@pytest.fixture
def fake_ssh(monkeypatch):
    built = []

    def factory(**kwargs):
        runner = FakeRunner(**kwargs)
        built.append(runner)
        return runner

    import k8s.ssh_runner as ssh_runner

    monkeypatch.setattr(ssh_runner, "SSHKubectlRunner", factory)
    return built


CLUSTER = {
    "id": "prod",
    "ssh_host": "master.example",
    "ssh_user": "kubeastra",
    "ssh_port": 22,
    "credential_ref": "cluster-ssh",
    "kubectl_context": "",
}


# ── resolving the credential ──────────────────────────────────────────────


def test_the_credential_is_found_under_the_mounted_directory(secret_dir):
    assert execution.credential_path_for(CLUSTER, secret_dir).endswith("cluster-ssh")


def test_a_missing_credential_file_is_reported_with_the_path(secret_dir):
    """"Cluster unreachable" is unactionable; the path and the field name are
    what an operator needs to fix it."""
    with pytest.raises(execution.ClusterUnreachable) as exc:
        execution.credential_path_for({**CLUSTER, "credential_ref": "absent"}, secret_dir)

    assert "credential_ref" in str(exc.value)


def test_a_credential_ref_cannot_escape_the_directory(secret_dir):
    """Registry rows are written through an API, so a row must not be able to
    name `../../etc/shadow` and have it read as a private key."""
    with pytest.raises(execution.ClusterUnreachable):
        execution.credential_path_for(
            {**CLUSTER, "credential_ref": "../../etc/passwd"}, secret_dir
        )


def test_an_empty_credential_ref_is_refused(secret_dir):
    with pytest.raises(execution.ClusterUnreachable):
        execution.credential_path_for({**CLUSTER, "credential_ref": ""}, secret_dir)


# ── building the runner ───────────────────────────────────────────────────


def test_the_runner_targets_the_registered_host(secret_dir, fake_ssh):
    execution.build_runner(CLUSTER, secret_dir)

    assert fake_ssh[0].kwargs["host"] == "master.example"
    assert fake_ssh[0].kwargs["username"] == "kubeastra"


def test_the_key_is_passed_as_a_path_not_read_into_memory(secret_dir, fake_ssh):
    """The runner opens it itself. Reading it here would put a private key in
    this process's heap and in any traceback that formats these arguments."""
    execution.build_runner(CLUSTER, secret_dir)

    assert fake_ssh[0].kwargs["credential_path"].endswith("cluster-ssh")
    assert "password" not in fake_ssh[0].kwargs


def test_a_kubectl_context_is_forwarded_when_set(secret_dir, fake_ssh):
    execution.build_runner({**CLUSTER, "kubectl_context": "admin@prod"}, secret_dir)

    assert fake_ssh[0].kwargs["context"] == "admin@prod"


# ── installing and tearing down ───────────────────────────────────────────


def test_kubectl_inside_the_block_uses_the_routed_runner(secret_dir, fake_ssh):
    before = get_runner()

    with execution.routed_execution(CLUSTER, secret_dir) as runner:
        assert get_runner() is runner
        assert get_runner() is not before


def test_the_default_runner_is_restored_afterwards(secret_dir, fake_ssh):
    before = get_runner()

    with execution.routed_execution(CLUSTER, secret_dir):
        pass

    assert get_runner() is before


def test_the_runner_is_closed_on_the_way_out(secret_dir, fake_ssh):
    """A leaked runner holds a socket, a transport thread and a session on
    someone else's sshd."""
    with execution.routed_execution(CLUSTER, secret_dir) as runner:
        assert runner.closed is False

    assert runner.closed is True


def test_an_exception_still_restores_and_closes(secret_dir, fake_ssh):
    """The failing investigation is exactly the case that leaks if teardown
    lives at the end of the block instead of in a finally."""
    before = get_runner()

    with pytest.raises(RuntimeError):
        with execution.routed_execution(CLUSTER, secret_dir) as runner:
            raise RuntimeError("investigation blew up")

    assert get_runner() is before
    assert runner.closed is True


def test_nested_routing_restores_the_outer_target(secret_dir, fake_ssh):
    """Not a shape the code uses today, but the contextvar token protocol only
    holds if reset is paired correctly — and getting it wrong leaves a runner
    installed for whatever runs next."""
    outer_cluster = {**CLUSTER, "id": "outer"}

    with execution.routed_execution(outer_cluster, secret_dir) as outer:
        with execution.routed_execution({**CLUSTER, "id": "inner"}, secret_dir):
            pass
        assert get_runner() is outer


# ── single-cluster mode must stay untouched ───────────────────────────────


def test_no_cluster_means_no_runner_is_installed(fake_ssh):
    """Single-cluster mode and every manual run take this path. It has to be
    free of side effects."""
    before = get_runner()

    with execution.routed_execution(None) as runner:
        assert runner is None
        assert get_runner() is before

    assert fake_ssh == []


# ── never falling back ────────────────────────────────────────────────────


def test_an_unreachable_cluster_raises_rather_than_using_the_default(secret_dir, fake_ssh):
    """The property this whole feature rests on. Falling back would produce a
    confident, fully-evidenced answer about the wrong machine — routing exists
    to prevent exactly that, so failing loudly is the correct outcome."""
    before = get_runner()

    with pytest.raises(execution.ClusterUnreachable):
        with execution.routed_execution({**CLUSTER, "credential_ref": "gone"}, secret_dir):
            pytest.fail("the block must not execute")

    assert get_runner() is before


def test_a_runner_that_fails_to_construct_is_reported_as_unreachable(secret_dir, monkeypatch):
    """Whatever paramiko raises, the caller sees one type it can act on."""
    import k8s.ssh_runner as ssh_runner

    def explode(**kwargs):
        raise OSError("no route to host")

    monkeypatch.setattr(ssh_runner, "SSHKubectlRunner", explode)

    with pytest.raises(execution.ClusterUnreachable) as exc:
        execution.build_runner(CLUSTER, secret_dir)

    assert "prod" in str(exc.value)


# ── what these tests do not prove ─────────────────────────────────────────


def test_the_ssh_path_itself_is_not_exercised_here():
    """Deliberate marker.

    Every test above swaps SSHKubectlRunner for a fake, so this file proves the
    wiring: the right host is selected, the runner is installed and torn down,
    and failure never falls back. It proves nothing about whether SSH to a real
    node authenticates, whether the account can run kubectl there, or how a
    half-open TCP connection behaves.

    Those need a real cluster. Until then, "cluster routing works" should be
    read as "the routing logic works".
    """
    import inspect

    source = inspect.getsource(sys.modules[__name__])
    assert "monkeypatch.setattr(ssh_runner" in source
