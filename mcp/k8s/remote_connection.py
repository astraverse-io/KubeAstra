"""Shared, sanitized failure taxonomy for remote diagnostic connections."""

from __future__ import annotations

from typing import Literal


RemoteFailureReason = Literal[
    "connect_timeout",
    "auth_failed",
    "host_key_mismatch",
    "host_key_missing",
    "identity_mismatch",
    "bastion_failed",
    "transport_error",
]

REMOTE_CONNECTION_REASONS: frozenset[str] = frozenset(
    {
        "connect_timeout",
        "auth_failed",
        "host_key_mismatch",
        "host_key_missing",
        "identity_mismatch",
        "bastion_failed",
        "transport_error",
    }
)

_SAFE_MESSAGES: dict[str, str] = {
    "connect_timeout": "SSH connection timed out",
    "auth_failed": "SSH authentication failed",
    "host_key_mismatch": "SSH host key verification failed",
    "host_key_missing": "SSH host key is not registered",
    "identity_mismatch": "Remote Kubernetes cluster identity did not match",
    "bastion_failed": "SSH bastion connection failed",
    "transport_error": "SSH transport failed",
}


class RemoteConnectionError(Exception):
    """Connection failure safe for API responses, metrics, and audit logs.

    Raw Paramiko/socket exceptions are deliberately not stored. They can contain
    destination addresses, usernames, credential paths, or library internals.
    """

    def __init__(self, reason: RemoteFailureReason):
        if reason not in REMOTE_CONNECTION_REASONS:
            raise ValueError(f"unknown remote connection reason: {reason!r}")
        self.reason = reason
        super().__init__(_SAFE_MESSAGES[reason])

    def __repr__(self) -> str:
        return f"RemoteConnectionError(reason={self.reason!r})"

