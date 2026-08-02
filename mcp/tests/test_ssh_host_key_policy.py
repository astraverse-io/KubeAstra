"""No SSH path may trust an unverified host key.

`SSHKubectlRunner` has always used `RejectPolicy` and is covered thoroughly by
`test_ssh_runner_secure.py`. `add_kubeconfig_context` did not: it opened its own
paramiko client with `AutoAddPolicy`, on the one code path whose purpose is to
send an operator's SSH password to a Kubernetes control-plane node.

`AutoAddPolicy` neither warns nor fails. A machine positioned between the
operator and the cluster presents its own key, is trusted immediately, and
receives the password. The only observable difference from a healthy connection
is that nothing appears to go wrong.

The README asserted the project "never uses Paramiko `AutoAddPolicy`" — true of
the runner, false of the codebase. A documented guarantee that does not hold is
worse than no guarantee, because it stops people looking.

These tests fail against the previous code.
"""

import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

paramiko = pytest.importorskip("paramiko")

from k8s.ssh_runner import HostKeyUnavailable, harden_host_keys  # noqa: E402


# ── the guard that stops this coming back ─────────────────────────────────


def test_no_module_uses_autoaddpolicy():
    """A single reintroduced call re-opens the hole with no visible symptom."""
    offenders = []
    roots = [MCP_DIR, MCP_DIR.parent / "ui" / "backend", MCP_DIR.parent / "cli"]
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if "_internal" in path.parts or "venv" in path.parts or "site-packages" in path.parts:
                continue
            if path.name == Path(__file__).name:
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                # `AutoAddPolicy(` is instantiation. Prose that merely names the
                # class — this docstring, the comment marking where it used to
                # be — is not a vulnerability, and a guard that cannot tell the
                # difference gets deleted the first time it cries wolf.
                if "AutoAddPolicy(" in line and not line.lstrip().startswith("#"):
                    offenders.append(f"{path.relative_to(MCP_DIR.parent)}:{lineno}")

    assert not offenders, (
        "AutoAddPolicy trusts any host key the far end presents. Use "
        "k8s.ssh_runner.harden_host_keys instead:\n  " + "\n  ".join(offenders)
    )


# ── the shared helper ─────────────────────────────────────────────────────


class _FakeClient:
    def __init__(self):
        self.policy = None
        self.loaded_path = None
        self.loaded_system = False

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def load_host_keys(self, path):
        self.loaded_path = path

    def load_system_host_keys(self):
        self.loaded_system = True


def test_the_policy_is_reject_not_autoadd(monkeypatch, tmp_path):
    known = tmp_path / "known_hosts"
    known.write_text("example.com ssh-ed25519 AAAATEST\n", encoding="utf-8")
    client = _FakeClient()

    harden_host_keys(client, known_hosts_path=str(known))

    assert isinstance(client.policy, paramiko.RejectPolicy)
    assert not isinstance(client.policy, paramiko.AutoAddPolicy)


def test_an_explicit_known_hosts_file_is_loaded(tmp_path):
    known = tmp_path / "known_hosts"
    known.write_text("example.com ssh-ed25519 AAAATEST\n", encoding="utf-8")
    client = _FakeClient()

    harden_host_keys(client, known_hosts_path=str(known))

    assert client.loaded_path == str(known)
    assert client.loaded_system is False


def test_a_missing_known_hosts_file_refuses_rather_than_falling_back(tmp_path):
    """Falling back to system keys here would silently widen what is trusted."""
    client = _FakeClient()

    with pytest.raises(HostKeyUnavailable) as excinfo:
        harden_host_keys(client, known_hosts_path=str(tmp_path / "nope"))

    assert "not found" in str(excinfo.value)
    assert client.loaded_system is False


def test_the_env_var_is_honoured(monkeypatch, tmp_path):
    known = tmp_path / "known_hosts"
    known.write_text("example.com ssh-ed25519 AAAATEST\n", encoding="utf-8")
    monkeypatch.setenv("SSH_KNOWN_HOSTS_PATH", str(known))
    client = _FakeClient()

    harden_host_keys(client)

    assert client.loaded_path == str(known)


def test_with_nothing_configured_it_uses_the_system_files(monkeypatch):
    monkeypatch.delenv("SSH_KNOWN_HOSTS_PATH", raising=False)
    client = _FakeClient()

    harden_host_keys(client)

    assert client.loaded_system is True
    assert isinstance(client.policy, paramiko.RejectPolicy)


# ── the caller that was vulnerable ────────────────────────────────────────


def test_the_password_is_only_offered_behind_reject_policy(monkeypatch, tmp_path):
    """The invariant that matters, stated the way paramiko actually works.

    Paramiko performs the host-key check *inside* ``connect()`` and raises
    before authenticating, so "connect must never be called" would be wrong —
    connect is *how* the check happens. What must hold is that by the time a
    password is handed over, the client is already refusing unknown keys.
    """
    from k8s import wrappers

    empty = tmp_path / "known_hosts"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("SSH_KNOWN_HOSTS_PATH", str(empty))

    seen = {}

    class _Client(_FakeClient):
        def connect(self, **kwargs):
            seen["policy"] = self.policy
            seen["password"] = kwargs.get("password")
            # What a real paramiko raises for a host absent from known_hosts.
            raise paramiko.SSHException(
                "Server 'untrusted.example.com' not found in known_hosts"
            )

        def close(self):
            pass

    monkeypatch.setattr(paramiko, "SSHClient", lambda: _Client())

    result = wrappers.add_kubeconfig_context("root@untrusted.example.com", password="hunter2")

    assert isinstance(seen["policy"], paramiko.RejectPolicy), (
        "the credential was offered to a client that had not been hardened"
    )
    assert result["success"] is False
    assert "known_hosts" in result["error"]
    assert "ssh-keyscan" in result["remediation"]


def test_the_refusal_says_how_to_fix_it(monkeypatch, tmp_path):
    """An operator who cannot act on the error will disable the check instead."""
    from k8s import wrappers

    monkeypatch.setenv("SSH_KNOWN_HOSTS_PATH", str(tmp_path / "definitely-absent"))

    class _Client(_FakeClient):
        def connect(self, **kwargs):  # pragma: no cover
            raise AssertionError("should not reach connect")

        def close(self):
            pass

    monkeypatch.setattr(paramiko, "SSHClient", lambda: _Client())

    result = wrappers.add_kubeconfig_context("root@untrusted.example.com", password="hunter2")

    assert result["success"] is False
    assert "ssh-keyscan" in result.get("remediation", "")
