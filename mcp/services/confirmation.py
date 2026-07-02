"""Confirmation token store for destructive Kubernetes operations.

Two-step ritual when `require_destructive_confirmation` is True:

  1. Caller invokes a destructive tool with `dry_run=True`.
     - kubectl runs with `--dry-run=server` and returns a preview.
     - We issue a single-use token bound to (operation, target fingerprint).
     - Token has a short TTL (default 60s) — fresh intent, not a long-lived
       capability.
  2. Caller invokes the tool again with `confirm=True, confirmation_token=<token>`.
     - We validate the token: operation match, fingerprint match, not expired,
       not previously consumed.
     - On success, the token is consumed (single-use) and the real kubectl
       call proceeds.

Token storage is in-memory and single-pod. Acceptable for v1 because:
  - The TTL is short (≤ a few minutes), so a pod restart at worst forces
    the caller to dry-run again.
  - Destructive ops are infrequent; cross-pod state isn't worth Redis yet.

If you move to multi-replica MCP, swap the dict for Redis (the public API
here is intentionally tiny: issue / consume / fingerprint).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from config.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass
class TokenRecord:
    token: str
    operation: str
    fingerprint: str
    issued_at: float
    expires_at: float
    issued_to: str  # user identity when available, else "anonymous"


class TokenStore:
    """Thread-safe in-memory token store."""

    def __init__(self):
        self._tokens: dict[str, TokenRecord] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        operation: str,
        fingerprint: str,
        ttl_seconds: int,
        user: Optional[str] = None,
    ) -> TokenRecord:
        token = secrets.token_urlsafe(24)
        now = time.time()
        record = TokenRecord(
            token=token,
            operation=operation,
            fingerprint=fingerprint,
            issued_at=now,
            expires_at=now + max(1, int(ttl_seconds)),
            issued_to=user or "anonymous",
        )
        with self._lock:
            self._tokens[token] = record
            self._purge_expired_locked()
        return record

    def consume(
        self,
        token: str,
        operation: str,
        fingerprint: str,
    ) -> tuple[Optional[TokenRecord], Optional[str]]:
        """Validate and consume a token.

        Returns (record, None) on success, (None, reason) on failure.
        Reason strings are intentionally generic to avoid leaking which
        check failed (defense in depth).
        """
        if not token:
            return None, "missing_token"

        with self._lock:
            self._purge_expired_locked()
            record = self._tokens.pop(token, None)

        if record is None:
            return None, "invalid_token"
        if record.operation != operation:
            return None, "operation_mismatch"
        if record.fingerprint != fingerprint:
            return None, "target_mismatch"
        if record.expires_at < time.time():
            return None, "expired"

        return record, None

    def _purge_expired_locked(self) -> None:
        now = time.time()
        for tok in [t for t, r in self._tokens.items() if r.expires_at < now]:
            self._tokens.pop(tok, None)


# Module-level singleton — destructive ops are infrequent and serialized
# enough that a single store is plenty.
token_store = TokenStore()


def fingerprint(operation: str, **target_kwargs: Any) -> str:
    """Stable hash of operation + sorted target kwargs.

    Used to bind a token to the exact operation it previewed: a token issued
    for `delete_pod ns=prod pod=api-1` cannot be used for `delete_pod ns=prod
    pod=api-2`. Kwargs are stringified and sorted for determinism.
    """
    parts = [operation]
    for k in sorted(target_kwargs):
        v = target_kwargs[k]
        parts.append(f"{k}={v}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def audit_token_event(
    event: str,  # "issued" | "consumed" | "rejected"
    operation: str,
    target_fingerprint: str,
    token_prefix: str = "",
    user: str = "anonymous",
    reason: Optional[str] = None,
) -> None:
    """Append a confirmation-token event to the audit log.

    Mirrors the format used by KubectlRunner._audit_log so all destructive-op
    history sits in one file.
    """
    settings = get_settings()
    if not settings.enable_audit_log:
        return

    try:
        ts = datetime.now().isoformat()
        parts = [
            ts,
            f"TOKEN_{event.upper()}",
            f"op={operation}",
            f"fp={target_fingerprint}",
            f"user={user}",
        ]
        if token_prefix:
            parts.append(f"token_prefix={token_prefix[:8]}")
        if reason:
            parts.append(f"reason={reason}")
        line = " | ".join(parts) + "\n"

        path = Path(settings.audit_log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(line)
    except Exception as exc:
        logger.warning("Failed to write token audit event: %s", exc)
