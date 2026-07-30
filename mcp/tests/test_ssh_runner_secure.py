"""Phase 3 security tests for the SSH kubectl runner."""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass
from pathlib import Path

import paramiko
import pytest

from k8s.kubectl_runner import KubectlTimeoutError
from k8s.remote_connection import REMOTE_CONNECTION_REASONS, RemoteConnectionError
from k8s.ssh_runner import (
    SSHBastionConfig,
    SSHKubectlRunner,
    _classify_connect_exception,
)


class _Server(paramiko.ServerInterface):
    def __init__(self, *, password: str, user_key: paramiko.PKey):
        self.password = password
        self.user_key = user_key

    def get_allowed_auths(self, username: str) -> str:
        return "publickey,password"

    def check_auth_password(self, username: str, password: str) -> int:
        return (
            paramiko.AUTH_SUCCESSFUL
            if username == "diagnostics" and password == self.password
            else paramiko.AUTH_FAILED
        )

    def check_auth_publickey(self, username: str, key: paramiko.PKey) -> int:
        return (
            paramiko.AUTH_SUCCESSFUL
            if username == "diagnostics" and key == self.user_key
            else paramiko.AUTH_FAILED
        )

    def check_channel_request(self, kind: str, chanid: int) -> int:
        return (
            paramiko.OPEN_SUCCEEDED
            if kind == "session"
            else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        )


@dataclass
class _SSHFixture:
    host: str
    port: int
    known_hosts: Path
    wrong_known_hosts: Path
    key_credentials: Path
    password_credentials: Path
    password: str
    user_key: paramiko.PKey
    stop: threading.Event
    listener: socket.socket
    thread: threading.Thread


def _known_hosts_line(name: str, key: paramiko.PKey) -> str:
    return f"{name} {key.get_name()} {key.get_base64()}\n"


@pytest.fixture
def ssh_server(tmp_path: Path):
    host_key = paramiko.RSAKey.generate(2048)
    wrong_host_key = paramiko.RSAKey.generate(2048)
    user_key = paramiko.RSAKey.generate(2048)
    password = "mounted-password-value"

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.1)
    host, port = listener.getsockname()
    alias = f"[qa17-control-plane]:{port}"

    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(_known_hosts_line(alias, host_key), encoding="utf-8")
    wrong_known_hosts = tmp_path / "wrong_known_hosts"
    wrong_known_hosts.write_text(
        _known_hosts_line(alias, wrong_host_key),
        encoding="utf-8",
    )

    key_credentials = tmp_path / "key-credentials"
    key_credentials.mkdir()
    user_key.write_private_key_file(str(key_credentials / "key"))
    (key_credentials / "key").chmod(0o400)

    password_credentials = tmp_path / "password-credentials"
    password_credentials.mkdir()
    (password_credentials / "password").write_text(password, encoding="utf-8")
    (password_credentials / "password").chmod(0o400)

    stop = threading.Event()

    def _serve() -> None:
        while not stop.is_set():
            try:
                client, _addr = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            transport = paramiko.Transport(client)
            try:
                transport.add_server_key(host_key)
                transport.start_server(
                    server=_Server(password=password, user_key=user_key)
                )
                while transport.is_active() and not stop.wait(0.01):
                    pass
            except (EOFError, OSError, paramiko.SSHException):
                pass
            finally:
                transport.close()
                client.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    fixture = _SSHFixture(
        host=host,
        port=port,
        known_hosts=known_hosts,
        wrong_known_hosts=wrong_known_hosts,
        key_credentials=key_credentials,
        password_credentials=password_credentials,
        password=password,
        user_key=user_key,
        stop=stop,
        listener=listener,
        thread=thread,
    )
    try:
        yield fixture
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=2)


def _runner(server: _SSHFixture, **overrides) -> SSHKubectlRunner:
    values = {
        "host": server.host,
        "username": "diagnostics",
        "port": server.port,
        "timeout": 2,
        "known_hosts_path": str(server.known_hosts),
        "known_hosts_alias": "qa17-control-plane",
        "credential_path": str(server.key_credentials),
    }
    values.update(overrides)
    return SSHKubectlRunner(**values)


def test_successful_mounted_private_key_authentication(ssh_server):
    runner = _runner(ssh_server)
    runner.connect()
    assert runner._client is not None  # noqa: SLF001
    runner.close()


def test_successful_mounted_password_authentication(ssh_server):
    runner = _runner(
        ssh_server,
        credential_path=str(ssh_server.password_credentials),
    )
    runner.connect()
    assert runner._client is not None  # noqa: SLF001
    runner.close()


def test_successful_encrypted_private_key_authentication(ssh_server, tmp_path):
    credentials = tmp_path / "encrypted-key-credentials"
    credentials.mkdir()
    ssh_server.user_key.write_private_key_file(
        str(credentials / "key"),
        password="key-passphrase",
    )
    (credentials / "key").chmod(0o400)
    (credentials / "passphrase").write_text(
        "key-passphrase",
        encoding="utf-8",
    )
    (credentials / "passphrase").chmod(0o400)

    runner = _runner(ssh_server, credential_path=str(credentials))
    runner.connect()
    runner.close()


def test_legacy_direct_password_authentication_remains_supported(ssh_server):
    runner = _runner(
        ssh_server,
        credential_path=None,
        password=ssh_server.password,
    )
    runner.connect()
    runner.close()


def test_host_key_mismatch_is_sanitized(ssh_server):
    runner = _runner(
        ssh_server,
        known_hosts_path=str(ssh_server.wrong_known_hosts),
    )
    with pytest.raises(RemoteConnectionError) as exc_info:
        runner.connect()
    assert exc_info.value.reason == "host_key_mismatch"
    assert ssh_server.host not in str(exc_info.value)


def test_missing_host_key_fails_closed_before_authentication(ssh_server, tmp_path):
    empty_known_hosts = tmp_path / "empty-known-hosts"
    empty_known_hosts.write_text("", encoding="utf-8")
    runner = _runner(ssh_server, known_hosts_path=str(empty_known_hosts))
    with pytest.raises(RemoteConnectionError) as exc_info:
        runner.connect()
    assert exc_info.value.reason == "host_key_missing"


def test_non_default_port_does_not_fall_back_to_plain_alias(tmp_path):
    host_key = paramiko.RSAKey.generate(1024)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(
        _known_hosts_line("qa17-control-plane", host_key),
        encoding="utf-8",
    )
    runner = SSHKubectlRunner(
        "127.0.0.1",
        "diagnostics",
        password="irrelevant",
        port=2222,
        known_hosts_path=str(known_hosts),
        known_hosts_alias="qa17-control-plane",
    )
    client = paramiko.SSHClient()
    with pytest.raises(RemoteConnectionError) as exc_info:
        runner._configure_host_keys(  # noqa: SLF001
            client,
            host=runner.host,
            port=runner.port,
            known_hosts_path=runner.known_hosts_path,
            known_hosts_alias=runner.known_hosts_alias,
        )
    assert exc_info.value.reason == "host_key_missing"


def test_authentication_failure_does_not_expose_password(ssh_server):
    runner = _runner(
        ssh_server,
        credential_path=None,
        password="incorrect-password-value",
    )
    with pytest.raises(RemoteConnectionError) as exc_info:
        runner.connect()
    assert exc_info.value.reason == "auth_failed"
    assert "incorrect-password-value" not in str(exc_info.value)
    assert "incorrect-password-value" not in repr(runner)


def test_private_key_permissions_fail_closed(ssh_server):
    (ssh_server.key_credentials / "key").chmod(0o644)
    runner = _runner(ssh_server)
    with pytest.raises(RemoteConnectionError) as exc_info:
        runner.connect()
    assert exc_info.value.reason == "auth_failed"


def test_credential_symlink_cannot_escape_mount(ssh_server, tmp_path):
    outside = tmp_path / "outside-key"
    ssh_server.user_key.write_private_key_file(str(outside))
    outside.chmod(0o400)
    credentials = tmp_path / "symlink-credentials"
    credentials.mkdir()
    (credentials / "key").symlink_to(outside)

    runner = _runner(ssh_server, credential_path=str(credentials))
    with pytest.raises(RemoteConnectionError) as exc_info:
        runner.connect()
    assert exc_info.value.reason == "auth_failed"


def test_remote_reason_taxonomy_matches_breaker_contract():
    assert REMOTE_CONNECTION_REASONS == {
        "connect_timeout",
        "auth_failed",
        "host_key_mismatch",
        "host_key_missing",
        "identity_mismatch",
        "bastion_failed",
        "transport_error",
    }


def test_transport_exception_classifier_is_bounded():
    host_key = paramiko.RSAKey.generate(1024)
    other_key = paramiko.RSAKey.generate(1024)
    cases = [
        (socket.timeout(), "connect_timeout"),
        (paramiko.AuthenticationException(), "auth_failed"),
        (
            paramiko.BadHostKeyException("host", host_key, other_key),
            "host_key_mismatch",
        ),
        (
            paramiko.SSHException("Server not found in known_hosts"),
            "host_key_missing",
        ),
        (paramiko.SSHException("authentication timed out"), "connect_timeout"),
        (OSError("connection refused at private-address"), "transport_error"),
    ]
    for exc, expected in cases:
        assert _classify_connect_exception(exc) == expected


class _Closable:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _OutputChannel:
    def __init__(self, stdout_chunks, stderr_chunks, *, returncode=0):
        self.stdout_chunks = list(stdout_chunks)
        self.stderr_chunks = list(stderr_chunks)
        self.returncode = returncode

    def recv_ready(self):
        return bool(self.stdout_chunks)

    def recv(self, size):
        return self.stdout_chunks.pop(0)

    def recv_stderr_ready(self):
        return bool(self.stderr_chunks)

    def recv_stderr(self, size):
        return self.stderr_chunks.pop(0)

    def exit_status_ready(self):
        return not self.stdout_chunks and not self.stderr_chunks

    def recv_exit_status(self):
        return self.returncode


def test_output_collector_drains_both_streams_and_caps_capture():
    channel = _OutputChannel(
        [b"stdout-1", b"stdout-2"],
        [b"stderr-1", b"stderr-2"],
        returncode=7,
    )
    stdout, stderr, rc, stdout_truncated, stderr_truncated = (
        SSHKubectlRunner._collect_channel_output(  # noqa: SLF001
            channel,
            stdout_limit=10,
            stderr_limit=9,
            timeout=1,
        )
    )
    assert stdout == b"stdout-1st"
    assert stderr == b"stderr-1s"
    assert rc == 7
    assert stdout_truncated is True
    assert stderr_truncated is True
    assert channel.stdout_chunks == []
    assert channel.stderr_chunks == []


def test_remote_command_uses_one_cumulative_timeout(monkeypatch):
    now = [50.0]
    observed = []
    channel = object()

    class _Stream:
        def __init__(self):
            self.channel = channel

    class _Client:
        def exec_command(self, command, timeout):
            assert timeout == 10
            now[0] += 4.0
            return _Stream(), _Stream(), _Stream()

    def fake_collect(channel_arg, **kwargs):
        assert channel_arg is channel
        observed.append(kwargs["timeout"])
        return b"{}", b"", 0, False, False

    monkeypatch.setattr("k8s.ssh_runner.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        SSHKubectlRunner,
        "_collect_channel_output",
        staticmethod(fake_collect),
    )
    runner = SSHKubectlRunner(
        "target.internal",
        "diagnostics",
        password="password",
        timeout=10,
    )
    runner._client = _Client()  # noqa: SLF001
    result = runner.run(["get", "pods"])
    assert result.returncode == 0
    assert observed == [6.0]


def test_operation_deadline_caps_reconnect_and_command(monkeypatch):
    now = [20.0]
    observed = []
    channel = object()

    class _Stream:
        def __init__(self):
            self.channel = channel

    class _Client:
        def exec_command(self, command, timeout):
            observed.append(("exec", timeout))
            now[0] += 1.0
            return _Stream(), _Stream(), _Stream()

    runner = SSHKubectlRunner(
        "target.internal",
        "diagnostics",
        password="password",
        timeout=10,
    )

    def fake_connect(timeout=None):
        observed.append(("connect", timeout))
        now[0] += 1.0
        runner._client = _Client()  # noqa: SLF001

    def fake_collect(channel_arg, **kwargs):
        observed.append(("collect", kwargs["timeout"]))
        return b"{}", b"", 0, False, False

    monkeypatch.setattr("k8s.ssh_runner.time.monotonic", lambda: now[0])
    monkeypatch.setattr(runner, "connect", fake_connect)
    monkeypatch.setattr(
        SSHKubectlRunner,
        "_collect_channel_output",
        staticmethod(fake_collect),
    )
    runner.set_operation_deadline(25.0)
    result = runner.run(["get", "pods"])
    assert result.returncode == 0
    assert observed == [
        ("connect", 5.0),
        ("exec", 4.0),
        ("collect", 3.0),
    ]


def test_expired_operation_deadline_does_not_reconnect(monkeypatch):
    monkeypatch.setattr("k8s.ssh_runner.time.monotonic", lambda: 50.0)
    runner = SSHKubectlRunner(
        "target.internal",
        "diagnostics",
        password="password",
        timeout=10,
    )
    runner.set_operation_deadline(49.0)
    called = False

    def fake_connect(timeout=None):
        nonlocal called
        called = True

    monkeypatch.setattr(runner, "connect", fake_connect)
    with pytest.raises(KubectlTimeoutError):
        runner.run(["get", "pods"])
    assert called is False


class _Outer(_Closable):
    def __init__(self, channel):
        super().__init__()
        self.channel = channel

    def get_transport(self):
        return self

    def is_active(self):
        return True

    def open_channel(self, *args, **kwargs):
        return self.channel


def test_bastion_resources_close_when_inner_connection_fails(monkeypatch):
    channel = _Closable()
    outer = _Outer(channel)
    calls = 0

    def fake_connect(self, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return outer
        raise RemoteConnectionError("auth_failed")

    monkeypatch.setattr(SSHKubectlRunner, "_connect_client", fake_connect)
    runner = SSHKubectlRunner(
        "target.internal",
        "diagnostics",
        password="target-password",
        known_hosts_path="/unused/target-known-hosts",
        bastion=SSHBastionConfig(
            host="bastion.internal",
            username="jump",
            password="bastion-password",
            known_hosts_path="/unused/bastion-known-hosts",
        ),
    )

    with pytest.raises(RemoteConnectionError) as exc_info:
        runner.connect()
    assert exc_info.value.reason == "auth_failed"
    assert channel.closed is True
    assert outer.closed is True


def test_run_propagates_remote_connection_error_from_reconnect(monkeypatch):
    """Regression: run()'s outer ``except Exception`` used to wrap every
    failure as ``KubectlError``, flattening ``RemoteConnectionError`` down to
    a generic transport error and destroying the sanitized reason label. The
    RemoteConnectionError must propagate so the router-side response and
    metrics keep the specific category (auth_failed, host_key_mismatch, ...).
    """
    runner = SSHKubectlRunner(
        "target.internal",
        "diagnostics",
        password="password",
        timeout=10,
    )

    def failing_ensure(timeout=None):
        raise RemoteConnectionError("auth_failed")

    monkeypatch.setattr(runner, "_ensure_connected", failing_ensure)
    with pytest.raises(RemoteConnectionError) as exc_info:
        runner.run(["get", "pods"])
    assert exc_info.value.reason == "auth_failed"


def test_run_shell_command_propagates_remote_connection_error(monkeypatch):
    runner = SSHKubectlRunner(
        "target.internal",
        "diagnostics",
        password="password",
        timeout=10,
    )

    def failing_ensure(timeout=None):
        raise RemoteConnectionError("host_key_mismatch")

    monkeypatch.setattr(runner, "_ensure_connected", failing_ensure)
    with pytest.raises(RemoteConnectionError) as exc_info:
        runner.run_shell_command(["helm", "status", "test"])
    assert exc_info.value.reason == "host_key_mismatch"


def test_bastion_uses_one_cumulative_connection_deadline(monkeypatch):
    channel = _Closable()
    outer = _Outer(channel)
    inner = _Closable()
    now = [100.0]
    observed_timeouts = []

    monkeypatch.setattr("k8s.ssh_runner.time.monotonic", lambda: now[0])

    def fake_connect(self, **kwargs):
        observed_timeouts.append(kwargs["operation_timeout"])
        now[0] += 4.0
        return outer if len(observed_timeouts) == 1 else inner

    monkeypatch.setattr(SSHKubectlRunner, "_connect_client", fake_connect)
    runner = SSHKubectlRunner(
        "target.internal",
        "diagnostics",
        password="target-password",
        timeout=10,
        known_hosts_path="/unused/target-known-hosts",
        bastion=SSHBastionConfig(
            host="bastion.internal",
            username="jump",
            password="bastion-password",
            known_hosts_path="/unused/bastion-known-hosts",
        ),
    )
    runner.connect()
    assert observed_timeouts == [10.0, 6.0]
    runner.close()
    assert inner.closed is True
    assert channel.closed is True
    assert outer.closed is True


# ── host-key lookup is case-sensitive in paramiko ─────────────────────────


def test_hostname_is_lowercased_for_host_key_lookup():
    """DNS is case-insensitive; paramiko's known_hosts lookup is not.

    An operator typing "S60503-gfs-k8s-m.example.com" into the connect dialog
    missed an entry stored as "s60503-…" and got "SSH host key is not
    registered" for a host they had trusted for months. OpenSSH lower-cases
    before matching; this makes us match that.
    """
    from k8s.ssh_runner import SSHKubectlRunner

    runner = SSHKubectlRunner(
        host="S60503-GFS-K8s-M.Example.COM", username="ansible", password="x"
    )
    assert runner.host == "s60503-gfs-k8s-m.example.com"


def test_host_key_name_lowercases_both_forms():
    from k8s.ssh_runner import SSHKubectlRunner

    runner = SSHKubectlRunner(host="Example.COM", username="u", password="x")
    assert runner._host_key_name("Example.COM", 22) == "example.com"
    assert runner._host_key_name("Example.COM", 2222) == "[example.com]:2222"
