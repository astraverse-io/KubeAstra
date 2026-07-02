"""Fail-closed SSH-based kubectl runner.

All remote host keys must already exist in a supplied ``known_hosts`` file or
the process' system known-hosts files. Target credentials may be passed by the
legacy interactive chat path as a password, or loaded from a read-only mounted
credential directory for machine remote diagnostics.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from config.settings import settings
from k8s.kubectl_runner import KubectlError, KubectlResult, KubectlTimeoutError
from k8s.remote_connection import (
    REMOTE_CONNECTION_REASONS,
    RemoteConnectionError,
    RemoteFailureReason,
)

logger = logging.getLogger(__name__)

DEFAULT_KNOWN_HOSTS_PATH_ENV = "SSH_KNOWN_HOSTS_PATH"
PRIVATE_KEY_FILENAME = "key"
PASSWORD_FILENAME = "password"
PASSPHRASE_FILENAME = "passphrase"
_MAX_PASSWORD_BYTES = 4096
_MAX_PRIVATE_KEY_BYTES = 1024 * 1024

# Backwards-compatible import used by routers/chat.py.
SSHConnectionError = RemoteConnectionError


@dataclass(frozen=True, slots=True)
class SSHBastionConfig:
    """Server-managed bastion configuration.

    The request body never constructs this object. Phase 4/6 will build it from
    reviewed target metadata and mounted Secret paths.
    """

    host: str
    username: str
    port: int = 22
    known_hosts_path: str | None = None
    known_hosts_alias: str | None = None
    credential_path: str | None = None
    password: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.host or not self.username:
            raise ValueError("bastion host and username are required")
        if (
            not isinstance(self.port, int)
            or isinstance(self.port, bool)
            or not 1 <= self.port <= 65535
        ):
            raise ValueError("bastion port must be an integer in [1, 65535]")
        if self.password is not None and self.credential_path is not None:
            raise ValueError(
                "bastion password and credential_path are mutually exclusive"
            )


@dataclass(frozen=True, slots=True)
class _AuthMaterial:
    password: str | None = field(default=None, repr=False)
    pkey: Any | None = field(default=None, repr=False)


def _safe_remote_error(reason: RemoteFailureReason) -> RemoteConnectionError:
    return RemoteConnectionError(reason)


def _classify_connect_exception(exc: BaseException) -> RemoteFailureReason:
    """Map a raw Paramiko/socket failure to the bounded public taxonomy."""
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "connect_timeout"

    try:
        import paramiko

        if isinstance(exc, paramiko.BadHostKeyException):
            return "host_key_mismatch"
        if isinstance(
            exc,
            (
                paramiko.AuthenticationException,
                paramiko.PasswordRequiredException,
            ),
        ):
            return "auth_failed"
        if isinstance(exc, paramiko.SSHException):
            text = str(exc).lower()
            if "known_hosts" in text or "known hosts" in text:
                return "host_key_missing"
            if "timed out" in text or "timeout" in text:
                return "connect_timeout"
            if isinstance(
                getattr(exc, "__cause__", None),
                (socket.timeout, TimeoutError),
            ):
                return "connect_timeout"
    except ImportError:
        pass

    return "transport_error"


def _read_secret_text(path: Path, *, max_bytes: int) -> str:
    """Read one mounted secret without exposing its path or contents in errors."""
    try:
        file_stat = path.stat()
        if not stat.S_ISREG(file_stat.st_mode):
            raise _safe_remote_error("auth_failed")
        # The Phase 3 contract requires credential files to be owner-only.
        if stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise _safe_remote_error("auth_failed")
        if file_stat.st_size > max_bytes:
            raise _safe_remote_error("auth_failed")
        data = path.read_bytes()
    except RemoteConnectionError:
        raise
    except (OSError, ValueError):
        raise _safe_remote_error("auth_failed") from None

    if len(data) > max_bytes:
        raise _safe_remote_error("auth_failed")
    try:
        value = data.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError:
        raise _safe_remote_error("auth_failed") from None
    if not value:
        raise _safe_remote_error("auth_failed")
    return value


def _validate_secret_file(path: Path, *, max_bytes: int) -> None:
    try:
        file_stat = path.stat()
    except OSError:
        raise _safe_remote_error("auth_failed") from None
    if not stat.S_ISREG(file_stat.st_mode):
        raise _safe_remote_error("auth_failed")
    if stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise _safe_remote_error("auth_failed")
    if file_stat.st_size <= 0 or file_stat.st_size > max_bytes:
        raise _safe_remote_error("auth_failed")


def _credential_child(root: Path, filename: str) -> Path:
    """Resolve one projected Secret entry and keep it inside its mount."""
    path = root / filename
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        raise _safe_remote_error("auth_failed") from None
    return resolved


def _load_private_key(path: Path, passphrase: str | None) -> Any:
    """Load the supported private-key formats without surfacing parser errors."""
    try:
        import paramiko
    except ImportError:
        raise _safe_remote_error("transport_error") from None

    _validate_secret_file(path, max_bytes=_MAX_PRIVATE_KEY_BYTES)
    key_types = [
        paramiko.Ed25519Key,
        paramiko.ECDSAKey,
        paramiko.RSAKey,
    ]
    dss_key = getattr(paramiko, "DSSKey", None)
    if dss_key is not None:
        key_types.append(dss_key)

    for key_type in key_types:
        try:
            return key_type.from_private_key_file(
                str(path),
                password=passphrase,
            )
        except paramiko.PasswordRequiredException:
            raise _safe_remote_error("auth_failed") from None
        except (paramiko.SSHException, ValueError, OSError):
            continue
    raise _safe_remote_error("auth_failed")


def _load_credential_directory(path: str) -> _AuthMaterial:
    """Load exactly one auth method from a mounted target credential directory."""
    try:
        root = Path(path).resolve(strict=True)
    except OSError:
        raise _safe_remote_error("auth_failed") from None
    if not root.is_dir():
        raise _safe_remote_error("auth_failed")

    key_link = root / PRIVATE_KEY_FILENAME
    password_link = root / PASSWORD_FILENAME
    passphrase_link = root / PASSPHRASE_FILENAME
    has_key = key_link.exists()
    has_password = password_link.exists()

    # Ambiguous credential directories are rejected rather than silently
    # changing authentication behavior.
    if has_key == has_password:
        raise _safe_remote_error("auth_failed")
    if passphrase_link.exists() and not has_key:
        raise _safe_remote_error("auth_failed")

    if has_key:
        key_path = _credential_child(root, PRIVATE_KEY_FILENAME)
        passphrase = (
            _read_secret_text(
                _credential_child(root, PASSPHRASE_FILENAME),
                max_bytes=_MAX_PASSWORD_BYTES,
            )
            if passphrase_link.exists()
            else None
        )
        return _AuthMaterial(pkey=_load_private_key(key_path, passphrase))
    return _AuthMaterial(
        password=_read_secret_text(
            _credential_child(root, PASSWORD_FILENAME),
            max_bytes=_MAX_PASSWORD_BYTES,
        )
    )


class SSHKubectlRunner:
    """Drop-in ``KubectlRunner`` replacement executing kubectl through SSH."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str | None = None,
        port: int = 22,
        timeout: Optional[float] = None,
        context: Optional[str] = None,
        *,
        known_hosts_path: str | None = None,
        known_hosts_alias: str | None = None,
        credential_path: str | None = None,
        bastion: SSHBastionConfig | None = None,
    ):
        if not host or not username:
            raise ValueError("host and username are required")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("port must be an integer in [1, 65535]")
        if password is not None and credential_path is not None:
            raise ValueError("password and credential_path are mutually exclusive")

        self.host = host
        self.username = username
        self._password = password
        self._credential_path = credential_path
        self.port = port
        self.timeout = float(timeout or settings.kubectl_timeout_seconds)
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        self.max_output_bytes = settings.max_output_bytes
        self.context = context
        self.known_hosts_path = (
            known_hosts_path
            or os.environ.get(DEFAULT_KNOWN_HOSTS_PATH_ENV)
            or None
        )
        self.known_hosts_alias = known_hosts_alias
        self.bastion = bastion
        # Optional cumulative deadline used by machine remote diagnostics.
        # Interactive SSH callers leave it unset and retain the legacy
        # per-command timeout behavior.
        self._operation_deadline_monotonic: float | None = None
        self._client = None
        self._bastion_client = None
        self._bastion_channel = None

    # ── Connection management ──────────────────────────────────────────────

    @staticmethod
    def _host_key_name(host: str, port: int) -> str:
        # OpenSSH's known_hosts format uses the plain hostname for the default
        # port and the bracketed ``[host]:port`` form otherwise. Do not bracket
        # port 22 entries; a bracketed port-22 line will not match the plain
        # form and vice versa, so operators who copy this convention into a
        # reviewed known_hosts file must follow it exactly.
        return host if port == 22 else f"[{host}]:{port}"

    def _configure_host_keys(
        self,
        client,
        *,
        host: str,
        port: int,
        known_hosts_path: str | None,
        known_hosts_alias: str | None,
    ) -> None:
        try:
            import paramiko
        except ImportError:
            raise _safe_remote_error("transport_error") from None

        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        if known_hosts_path:
            try:
                path = Path(known_hosts_path)
                if not path.is_file():
                    raise _safe_remote_error("host_key_missing")
                client.load_host_keys(str(path))
            except RemoteConnectionError:
                raise
            except (OSError, ValueError, paramiko.SSHException):
                raise _safe_remote_error("host_key_missing") from None
        else:
            # Legacy /api/chat callers use the operator's system known-hosts
            # files. RejectPolicy still fails closed when the target is absent.
            try:
                client.load_system_host_keys()
            except (OSError, paramiko.SSHException):
                raise _safe_remote_error("host_key_missing") from None

        if known_hosts_alias:
            destination_name = self._host_key_name(host, port)
            # OpenSSH treats an unbracketed host entry as port 22. Do not let a
            # non-default-port target fall back to that entry: the reviewed
            # known_hosts file must pin the exact [alias]:port endpoint.
            alias_candidates = [self._host_key_name(known_hosts_alias, port)]
            alias_keys = None
            for alias in dict.fromkeys(alias_candidates):
                alias_keys = client.get_host_keys().lookup(alias)
                if alias_keys:
                    break
            if not alias_keys:
                raise _safe_remote_error("host_key_missing")
            for key_type, key in alias_keys.items():
                client.get_host_keys().add(destination_name, key_type, key)
        elif known_hosts_path:
            destination_name = self._host_key_name(host, port)
            if not client.get_host_keys().lookup(destination_name):
                raise _safe_remote_error("host_key_missing")

    def _auth_material(
        self,
        *,
        password: str | None,
        credential_path: str | None,
    ) -> _AuthMaterial:
        if credential_path is not None:
            return _load_credential_directory(credential_path)
        if password:
            return _AuthMaterial(password=password)
        raise _safe_remote_error("auth_failed")

    def _connect_client(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str | None,
        credential_path: str | None,
        known_hosts_path: str | None,
        known_hosts_alias: str | None,
        sock=None,
        bastion_phase: bool = False,
        operation_timeout: float | None = None,
    ):
        try:
            import paramiko
        except ImportError:
            raise _safe_remote_error(
                "bastion_failed" if bastion_phase else "transport_error"
            ) from None

        client = paramiko.SSHClient()
        connect_timeout = (
            self.timeout if operation_timeout is None else operation_timeout
        )
        if connect_timeout <= 0:
            raise _safe_remote_error(
                "bastion_failed" if bastion_phase else "connect_timeout"
            )
        try:
            self._configure_host_keys(
                client,
                host=host,
                port=port,
                known_hosts_path=known_hosts_path,
                known_hosts_alias=known_hosts_alias,
            )
            auth = self._auth_material(
                password=password,
                credential_path=credential_path,
            )
            client.connect(
                hostname=host,
                port=port,
                username=username,
                password=auth.password,
                pkey=auth.pkey,
                timeout=connect_timeout,
                banner_timeout=connect_timeout,
                auth_timeout=connect_timeout,
                channel_timeout=connect_timeout,
                look_for_keys=False,
                allow_agent=False,
                sock=sock,
            )
            return client
        except RemoteConnectionError:
            try:
                client.close()
            except Exception:
                pass
            if bastion_phase:
                raise _safe_remote_error("bastion_failed") from None
            raise
        except Exception as exc:
            try:
                client.close()
            except Exception:
                pass
            reason: RemoteFailureReason = (
                "bastion_failed"
                if bastion_phase
                else _classify_connect_exception(exc)
            )
            raise _safe_remote_error(reason) from None

    def connect(self, timeout: float | None = None) -> None:
        """Open and verify the SSH connection. Idempotent per runner."""
        if self._client is not None:
            return

        connect_timeout = self.timeout if timeout is None else float(timeout)
        if connect_timeout <= 0:
            raise _safe_remote_error("connect_timeout")
        deadline = time.monotonic() + connect_timeout

        def _remaining(reason: RemoteFailureReason) -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _safe_remote_error(reason)
            return remaining

        if self.bastion is None:
            self._client = self._connect_client(
                host=self.host,
                port=self.port,
                username=self.username,
                password=self._password,
                credential_path=self._credential_path,
                known_hosts_path=self.known_hosts_path,
                known_hosts_alias=self.known_hosts_alias,
                operation_timeout=_remaining("connect_timeout"),
            )
        else:
            outer = None
            channel = None
            inner = None
            try:
                outer = self._connect_client(
                    host=self.bastion.host,
                    port=self.bastion.port,
                    username=self.bastion.username,
                    password=self.bastion.password,
                    credential_path=self.bastion.credential_path,
                    known_hosts_path=self.bastion.known_hosts_path,
                    known_hosts_alias=self.bastion.known_hosts_alias,
                    bastion_phase=True,
                    operation_timeout=_remaining("bastion_failed"),
                )
                transport = outer.get_transport()
                if transport is None or not transport.is_active():
                    raise _safe_remote_error("bastion_failed")
                try:
                    channel = transport.open_channel(
                        "direct-tcpip",
                        (self.host, self.port),
                        ("127.0.0.1", 0),
                        timeout=_remaining("bastion_failed"),
                    )
                except Exception:
                    raise _safe_remote_error("bastion_failed") from None
                inner = self._connect_client(
                    host=self.host,
                    port=self.port,
                    username=self.username,
                    password=self._password,
                    credential_path=self._credential_path,
                    known_hosts_path=self.known_hosts_path,
                    known_hosts_alias=self.known_hosts_alias,
                    sock=channel,
                    operation_timeout=_remaining("connect_timeout"),
                )
            except Exception:
                if inner is not None:
                    inner.close()
                if channel is not None:
                    channel.close()
                if outer is not None:
                    outer.close()
                raise
            self._bastion_client = outer
            self._bastion_channel = channel
            self._client = inner

        logger.info(
            "SSH connected target=%s user=%s port=%s bastion=%s",
            self.host,
            self.username,
            self.port,
            self.bastion is not None,
        )

    def close(self) -> None:
        """Close inner, forwarding channel, and bastion in dependency order."""
        client, self._client = self._client, None
        channel, self._bastion_channel = self._bastion_channel, None
        bastion, self._bastion_client = self._bastion_client, None
        for resource in (client, channel, bastion):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass

    def _invalidate_connection(self) -> None:
        self.close()

    def _ensure_connected(self, timeout: float | None = None) -> None:
        if self._client is None:
            self.connect(timeout=timeout)

    def set_operation_deadline(self, deadline_monotonic: float | None) -> None:
        """Set a cumulative deadline shared by subsequent remote commands.

        The caller supplies an absolute ``time.monotonic()`` value. Every
        kubectl/Helm operation receives the smaller of its normal timeout and
        the remaining cumulative budget. Passing ``None`` restores legacy
        per-command timeout behavior.
        """
        if deadline_monotonic is not None and deadline_monotonic <= 0:
            raise ValueError("deadline_monotonic must be positive")
        self._operation_deadline_monotonic = deadline_monotonic

    def _operation_timeout(self, requested_timeout: float | None = None) -> float:
        timeout = self.timeout if requested_timeout is None else float(requested_timeout)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if self._operation_deadline_monotonic is not None:
            timeout = min(
                timeout,
                self._operation_deadline_monotonic - time.monotonic(),
            )
        if timeout <= 0:
            raise KubectlTimeoutError(
                "remote diagnostic deadline expired",
                -1,
                "Remote diagnostic deadline expired",
            )
        return timeout

    # ── kubectl execution ──────────────────────────────────────────────────

    @staticmethod
    def _collect_channel_output(
        channel,
        *,
        stdout_limit: int,
        stderr_limit: int,
        timeout: float,
    ) -> tuple[bytes, bytes, int, bool, bool]:
        """Drain stdout and stderr concurrently until the command exits.

        Reading one Paramiko file object to EOF before the other can deadlock
        when the remote process fills the opposite stream's SSH window. Keep
        draining both streams even after the capture cap is reached; excess
        bytes are discarded so the remote command can exit normally.
        """
        if stdout_limit < 0 or stderr_limit < 0:
            raise ValueError("output limits must be non-negative")
        if timeout <= 0:
            raise socket.timeout("SSH command deadline expired")

        stdout = bytearray()
        stderr = bytearray()
        stdout_truncated = False
        stderr_truncated = False
        deadline = time.monotonic() + timeout

        def _capture(buffer: bytearray, chunk: bytes, limit: int) -> bool:
            available = max(0, limit - len(buffer))
            if available:
                buffer.extend(chunk[:available])
            return len(chunk) > available

        while True:
            made_progress = False
            while channel.recv_ready():
                chunk = channel.recv(32768)
                if not chunk:
                    break
                made_progress = True
                stdout_truncated = (
                    _capture(stdout, chunk, stdout_limit) or stdout_truncated
                )
            while channel.recv_stderr_ready():
                chunk = channel.recv_stderr(32768)
                if not chunk:
                    break
                made_progress = True
                stderr_truncated = (
                    _capture(stderr, chunk, stderr_limit) or stderr_truncated
                )

            if channel.exit_status_ready():
                # Paramiko may expose the exit status before the final buffered
                # packets are consumed. Loop until both queues are empty.
                if not channel.recv_ready() and not channel.recv_stderr_ready():
                    break

            if time.monotonic() >= deadline:
                raise socket.timeout("SSH command output collection timed out")
            if not made_progress:
                time.sleep(0.005)

        return (
            bytes(stdout),
            bytes(stderr),
            channel.recv_exit_status(),
            stdout_truncated,
            stderr_truncated,
        )

    def run(
        self,
        args: List[str],
        namespace: Optional[str] = None,
        capture_output: bool = True,  # ignored — SSH always captures
        max_output: Optional[int] = None,
        stdin_data: Optional[str] = None,
    ) -> KubectlResult:
        operation_timeout = self._operation_timeout()
        command_deadline = time.monotonic() + operation_timeout
        self._ensure_connected(timeout=operation_timeout)
        operation_timeout = command_deadline - time.monotonic()
        if operation_timeout <= 0:
            raise KubectlTimeoutError(
                "remote diagnostic deadline expired during reconnect",
                -1,
                "Remote diagnostic deadline expired",
            )

        parts: List[str] = ["kubectl"]
        if self.context:
            parts.extend(["--context", self.context])
        if namespace:
            parts.extend(["-n", namespace])
        parts.extend(args)

        cmd_str = " ".join(self._quote(p) for p in parts)
        logger.info("SSH kubectl target=%s command=%s", self.host, cmd_str)

        limit = max_output if max_output is not None else self.max_output_bytes
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("max_output must be a non-negative integer")
        start = datetime.now()
        try:
            stdin_ch, stdout_ch, stderr_ch = self._client.exec_command(
                cmd_str,
                timeout=operation_timeout,
            )
            if stdin_data:
                stdin_ch.write(stdin_data)
                stdin_ch.channel.shutdown_write()

            (
                raw_out,
                raw_err,
                returncode,
                stdout_truncated,
                stderr_truncated,
            ) = self._collect_channel_output(
                stdout_ch.channel,
                stdout_limit=limit,
                stderr_limit=self.max_output_bytes,
                timeout=max(0.0, command_deadline - time.monotonic()),
            )
            duration = (datetime.now() - start).total_seconds()
            stdout = raw_out.decode("utf-8", errors="replace")
            stderr = raw_err.decode("utf-8", errors="replace")
            if stdout_truncated:
                stdout += "\n[... output truncated ...]"
            if stderr_truncated:
                stderr += "\n[... output truncated ...]"

            return KubectlResult(
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                command=parts,
                duration_seconds=duration,
                truncated=stdout_truncated or stderr_truncated,
            )
        except (KubectlError, KubectlTimeoutError):
            raise
        except RemoteConnectionError:
            # Reconnect failures carry a sanitized reason label (auth_failed,
            # host_key_mismatch, ...) that the router-side metrics and
            # response body preserve. Wrapping them as a generic KubectlError
            # would flatten every category to "transport_error" and mislead
            # the operator.
            self._invalidate_connection()
            raise
        except (socket.timeout, TimeoutError):
            self._invalidate_connection()
            raise KubectlTimeoutError(
                f"remote kubectl timed out after {operation_timeout}s",
                -1,
                "SSH command exceeded its timeout",
            ) from None
        except Exception:
            self._invalidate_connection()
            raise KubectlError(
                "remote kubectl transport failed",
                -1,
                "SSH transport failed",
            ) from None

    def run_json(
        self,
        args: List[str],
        namespace: Optional[str] = None,
    ) -> dict:
        if "-o" not in args and "--output" not in args:
            args = list(args) + ["-o", "json"]

        result = self.run(
            args,
            namespace=namespace,
            max_output=10 * 1024 * 1024,
        )
        result.raise_for_status()
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            raise KubectlError(
                "failed to parse remote kubectl JSON",
                result.returncode,
                "invalid JSON returned by kubectl",
            ) from None

    def run_shell_command(
        self,
        cmd_parts: List[str],
        timeout: Optional[float] = None,
    ) -> tuple[str, str, int]:
        """Execute an allowlisted Helm/read-only command on the remote target."""
        exec_timeout = self._operation_timeout(timeout)
        command_deadline = time.monotonic() + exec_timeout
        self._ensure_connected(timeout=exec_timeout)
        exec_timeout = command_deadline - time.monotonic()
        if exec_timeout <= 0:
            raise KubectlTimeoutError(
                "remote diagnostic deadline expired during reconnect",
                -1,
                "Remote diagnostic deadline expired",
            )
        cmd_str = " ".join(self._quote(p) for p in cmd_parts)
        try:
            _stdin, stdout_ch, stderr_ch = self._client.exec_command(
                cmd_str,
                timeout=exec_timeout,
            )
            (
                raw_out,
                raw_err,
                returncode,
                stdout_truncated,
                stderr_truncated,
            ) = self._collect_channel_output(
                stdout_ch.channel,
                stdout_limit=self.max_output_bytes,
                stderr_limit=self.max_output_bytes,
                timeout=max(0.0, command_deadline - time.monotonic()),
            )
            stdout = raw_out.decode("utf-8", errors="replace")
            stderr = raw_err.decode("utf-8", errors="replace")
            if stdout_truncated:
                stdout += "\n[... output truncated ...]"
            if stderr_truncated:
                stderr += "\n[... output truncated ...]"
            return stdout, stderr, returncode
        except RemoteConnectionError:
            # See run() for rationale — preserve the sanitized reason label.
            self._invalidate_connection()
            raise
        except (socket.timeout, TimeoutError):
            self._invalidate_connection()
            raise KubectlTimeoutError(
                f"remote command timed out after {exec_timeout}s",
                -1,
                "SSH command exceeded its timeout",
            ) from None
        except Exception:
            self._invalidate_connection()
            raise KubectlError(
                "remote command transport failed",
                -1,
                "SSH transport failed",
            ) from None

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _quote(value: str) -> str:
        if value and all(c.isalnum() or c in "-_./=,:+" for c in value):
            return value
        return "'" + value.replace("'", "'\\''") + "'"

    def __repr__(self) -> str:
        return (
            f"SSHKubectlRunner(host={self.host!r}, "
            f"user={self.username!r}, port={self.port!r}, "
            f"bastion={self.bastion is not None!r})"
        )


__all__ = [
    "REMOTE_CONNECTION_REASONS",
    "RemoteConnectionError",
    "SSHBastionConfig",
    "SSHConnectionError",
    "SSHKubectlRunner",
]
