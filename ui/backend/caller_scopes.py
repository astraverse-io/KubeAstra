"""Caller-scope registry for the Ansible remote-diagnostics API.

Defines who may invoke ``/api/v1/agent/invoke`` against which registered
targets. Each scope (`qa-ansible`, `staging-ansible`, etc.) carries:

  * a current token (required) and an optional previous token, both read from
    files projected by a Kubernetes Secret into the pod;
  * a per-scope ``allowed_target_ids`` allowlist.

The legacy ``AGENT_API_TOKEN`` keeps working for target-less invocations: it
resolves to the ``LEGACY_SCOPE_NAME`` sentinel, which has no target access.

Two types are exposed:

  * ``CallerScope`` is the in-registry record. It carries token material;
    fields are marked ``repr=False`` so default logging/tracing cannot
    serialize them. This type stays inside the auth layer.
  * ``Principal`` is the sanitized post-authentication identity. It contains
    no token material and is safe to log, store on request state, or pass
    into worker functions.

Tokens are compared with ``hmac.compare_digest`` (constant-time per call). To
prevent timing channels across scopes, the matcher iterates every scope
unconditionally before deciding the outcome.

Plan refs: §8 (Caller Authentication and Authorization), §14 (audit), §7.2
(credential mount root for the Secret projection target).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from target_registry import _UniqueKeySafeLoader  # reuse duplicate-key loader


_SCOPE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_TARGET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

DEFAULT_TOKEN_MOUNT_ROOT = "/var/run/agent-caller-tokens"

# Sentinel returned for the legacy unscoped AGENT_API_TOKEN. Tagged so the
# authorize_target check can refuse remote diagnostics without special-casing
# elsewhere in the call path.
LEGACY_SCOPE_NAME = "__legacy_unscoped__"


class ScopeRegistryError(ValueError):
    """Raised when the caller-scopes file is malformed."""


def token_fingerprint(token: str) -> str:
    """Centralized 8-char SHA-256 prefix used everywhere a fingerprint
    surfaces externally: principal records, metric labels, audit entries,
    and log lines. Non-reversible; the small collision probability at this
    cardinality is acceptable for observability but not for authorization
    decisions — see ``_token_digest`` for that."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def _token_digest(token: str) -> str:
    """Full SHA-256 digest used for duplicate-token detection at registry
    load time. The truncation in ``token_fingerprint`` is deliberately not
    used here: collision in the truncated prefix would make authorization
    order-dependent, exactly the bug duplicate detection exists to prevent."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Principal:
    """Sanitized post-authentication identity.

    Carries no token material. Safe to log, store on request state, or pass
    into worker functions. Constructed by ``ScopeRegistry.identify_token``
    after a successful bearer match.
    """

    scope_name: str
    token_fingerprint: str
    allowed_target_ids: frozenset[str]

    @property
    def is_legacy(self) -> bool:
        return self.scope_name == LEGACY_SCOPE_NAME

    def allows_target(self, target_id: str) -> bool:
        """Whether this principal's allowlist contains ``target_id``. The
        target's own ``allowed_caller_scopes`` must also list this principal's
        scope_name — authorization requires both sides. See
        ``authorize_target``."""
        return target_id in self.allowed_target_ids


@dataclass(frozen=True, slots=True)
class CallerScope:
    """Internal registry record holding token material.

    ``repr=False`` on the token fields prevents default dataclass repr (and
    therefore most log formatters and tracing systems) from serializing the
    secret values. Public authentication results return ``Principal``
    instances, never ``CallerScope``.
    """

    name: str
    current_token: str = field(repr=False)
    previous_token: str | None = field(repr=False)
    allowed_target_ids: frozenset[str]

    @property
    def is_legacy(self) -> bool:
        return self.name == LEGACY_SCOPE_NAME

    def to_principal(self, presented_token: str) -> Principal:
        return Principal(
            scope_name=self.name,
            token_fingerprint=token_fingerprint(presented_token),
            allowed_target_ids=self.allowed_target_ids,
        )


def _legacy_scope(token: str, previous_token: str | None) -> CallerScope:
    return CallerScope(
        name=LEGACY_SCOPE_NAME,
        current_token=token,
        previous_token=previous_token,
        allowed_target_ids=frozenset(),
    )


@dataclass(frozen=True, slots=True)
class ScopeRegistry:
    """Immutable collection of caller scopes plus the optional legacy scope.

    Use ``identify_token`` to match a presented bearer token against the
    registry's full set of (current, previous) tokens in constant time.
    """

    scopes: Mapping[str, CallerScope]
    legacy: CallerScope | None

    def __post_init__(self) -> None:
        if not isinstance(self.scopes, MappingProxyType):
            object.__setattr__(self, "scopes", MappingProxyType(dict(self.scopes)))

    def get(self, name: str) -> CallerScope | None:
        if name == LEGACY_SCOPE_NAME:
            return self.legacy
        return self.scopes.get(name)

    def __len__(self) -> int:
        return len(self.scopes) + (1 if self.legacy is not None else 0)

    def has_any_principal(self) -> bool:
        """Whether at least one scope or the legacy token is configured."""
        return len(self.scopes) > 0 or self.legacy is not None

    def identify_token(self, presented: str) -> Principal | None:
        """Match ``presented`` against every (current, previous) token across
        every scope, including legacy. Returns a sanitized ``Principal``
        (no token material) or None.

        Implementation detail: every scope is compared unconditionally so the
        wall-clock cost does not depend on which scope (or rotation slot)
        matches. ``hmac.compare_digest`` is constant-time per call.
        """
        matched_scope: CallerScope | None = None

        if not presented:
            # Walk every scope so an empty bearer header takes the same
            # wall-clock cost as a malformed one.
            for scope in self.scopes.values():
                hmac.compare_digest(presented, scope.current_token)
                if scope.previous_token is not None:
                    hmac.compare_digest(presented, scope.previous_token)
            if self.legacy is not None:
                hmac.compare_digest(presented, self.legacy.current_token)
                if self.legacy.previous_token is not None:
                    hmac.compare_digest(presented, self.legacy.previous_token)
            return None

        for scope in self.scopes.values():
            if hmac.compare_digest(presented, scope.current_token):
                if matched_scope is None:
                    matched_scope = scope
            if scope.previous_token is not None:
                if hmac.compare_digest(presented, scope.previous_token):
                    if matched_scope is None:
                        matched_scope = scope
        if self.legacy is not None:
            if hmac.compare_digest(presented, self.legacy.current_token):
                if matched_scope is None:
                    matched_scope = self.legacy
            if self.legacy.previous_token is not None:
                if hmac.compare_digest(presented, self.legacy.previous_token):
                    if matched_scope is None:
                        matched_scope = self.legacy
        if matched_scope is None:
            return None
        return matched_scope.to_principal(presented)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ScopeRegistryError(message)


def _read_token_file(path: str, token_mount_root: str) -> str:
    """Read a token file from inside ``token_mount_root``.

    Validates the path stays within the configured root (prevents a
    misconfigured ConfigMap from pointing at /etc/shadow). Returns the
    stripped contents. The file MUST exist and be non-empty — optionality
    belongs to the YAML field, not the file: a configured ``previous_token_path``
    means the operator intends to have a previous token, so a missing
    projected Secret key is a broken rotation rollout, not a silent fall-back.
    """
    resolved = Path(path).resolve()
    root_resolved = Path(token_mount_root).resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ScopeRegistryError(
            f"token path {path!r} is outside the token mount root {token_mount_root!r}"
        ) from exc
    if not resolved.exists():
        raise ScopeRegistryError(f"token file does not exist: {path}")
    try:
        token = resolved.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ScopeRegistryError(f"could not read token file {path}: {exc}") from exc
    if not token:
        raise ScopeRegistryError(f"token file is empty: {path}")
    return token


def _parse_scope(
    name: str,
    raw: Mapping[str, Any],
    token_mount_root: str,
) -> CallerScope:
    _require(isinstance(raw, dict), f"{name}: scope entry must be a mapping")
    _require(
        _SCOPE_NAME_RE.match(name) is not None,
        f"{name}: scope name must match {_SCOPE_NAME_RE.pattern}",
    )
    _require(
        name != LEGACY_SCOPE_NAME,
        f"{name}: scope name {LEGACY_SCOPE_NAME!r} is reserved",
    )

    current_path = raw.get("current_token_path")
    _require(
        isinstance(current_path, str) and current_path,
        f"{name}: current_token_path is required",
    )
    current_token = _read_token_file(current_path, token_mount_root)

    previous_path = raw.get("previous_token_path")
    previous_token: str | None = None
    if previous_path is not None:
        _require(
            isinstance(previous_path, str) and previous_path,
            f"{name}: previous_token_path must be a string when set",
        )
        # YAML field present → operator intends a previous token. A missing
        # projected file is a broken rotation rollout, not silent fall-back.
        previous_token = _read_token_file(previous_path, token_mount_root)

    allowed = raw.get("allowed_target_ids")
    _require(
        isinstance(allowed, list) and allowed,
        f"{name}: allowed_target_ids must be a non-empty list",
    )
    allow_set: set[str] = set()
    for tid in allowed:
        _require(
            isinstance(tid, str) and _TARGET_ID_RE.match(tid) is not None,
            f"{name}: invalid target_id {tid!r} in allowed_target_ids",
        )
        allow_set.add(tid)

    return CallerScope(
        name=name,
        current_token=current_token,
        previous_token=previous_token,
        allowed_target_ids=frozenset(allow_set),
    )


def _detect_token_collisions(
    scopes: Mapping[str, CallerScope],
    legacy: CallerScope | None,
) -> None:
    """Reject the registry if any (current, previous) token value is shared
    across two scopes or between a scope and the legacy slot.

    Without this check, ``identify_token`` would return whichever scope
    iterates first — making authorization order-dependent and confusing.
    Errors report fingerprints only, never the token values.
    """
    # Each claim is owner:slot ("qa-ansible:current"). Two claims sharing a
    # fingerprint are a collision regardless of whether the owners match —
    # current == previous within one scope is also a misconfiguration.
    seen: dict[str, str] = {}

    def _claim(token: str | None, owner: str, slot: str) -> None:
        if token is None:
            return
        digest = _token_digest(token)
        claim_id = f"{owner}:{slot}"
        if digest in seen:
            # Error message uses the short fingerprint (8 chars) for human
            # readability; the full digest stays internal. Token values
            # never appear in this string.
            raise ScopeRegistryError(
                f"duplicate token: {seen[digest]!r} and {claim_id!r} share a "
                f"token value (fingerprint={token_fingerprint(token)}); tokens "
                f"must be unique"
            )
        seen[digest] = claim_id

    for scope_name, scope in scopes.items():
        _claim(scope.current_token, scope_name, "current")
        _claim(scope.previous_token, scope_name, "previous")
    if legacy is not None:
        _claim(legacy.current_token, LEGACY_SCOPE_NAME, "current")
        _claim(legacy.previous_token, LEGACY_SCOPE_NAME, "previous")


def load_registry(
    path: str | Path,
    *,
    token_mount_root: str = DEFAULT_TOKEN_MOUNT_ROOT,
    legacy_current_token: str | None = None,
    legacy_previous_token: str | None = None,
) -> ScopeRegistry:
    """Parse and validate the caller-scopes YAML.

    A missing file returns a registry containing only the legacy slot (if
    provided) — useful during the rollout window when the chart has not yet
    projected scoped tokens. The legacy unscoped ``AGENT_API_TOKEN`` is
    registered when ``legacy_current_token`` is non-empty; it never has
    target access.

    Raises:
        ScopeRegistryError: when the file exists but contains invalid entries,
        any referenced token file is missing/empty, or any two scopes (or a
        scope and legacy) share a token value.
    """
    legacy = (
        _legacy_scope(legacy_current_token, legacy_previous_token)
        if legacy_current_token
        else None
    )

    p = Path(path)
    if not p.exists():
        _detect_token_collisions({}, legacy)
        return ScopeRegistry(scopes={}, legacy=legacy)
    try:
        raw = yaml.load(p.read_text(), Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise ScopeRegistryError(
            f"caller-scopes file {p} is not valid YAML: {exc}"
        ) from exc

    if raw is None:
        _detect_token_collisions({}, legacy)
        return ScopeRegistry(scopes={}, legacy=legacy)
    _require(isinstance(raw, dict), "caller-scopes root must be a mapping")
    block = raw.get("callerScopes")
    _require(isinstance(block, dict), "caller-scopes file must have a 'callerScopes' mapping")

    parsed: dict[str, CallerScope] = {}
    for name, entry in block.items():
        _require(isinstance(name, str), f"scope names must be strings (got {name!r})")
        if name in parsed:
            raise ScopeRegistryError(f"duplicate scope name: {name}")
        parsed[name] = _parse_scope(name, entry, token_mount_root)

    _detect_token_collisions(parsed, legacy)
    return ScopeRegistry(scopes=parsed, legacy=legacy)


_lock = threading.Lock()
_active: ScopeRegistry | None = None


def set_active(registry: ScopeRegistry) -> None:
    global _active
    with _lock:
        _active = registry


def get_active() -> ScopeRegistry:
    with _lock:
        return _active if _active is not None else ScopeRegistry(scopes={}, legacy=None)


# ── Authorization ────────────────────────────────────────────────────────────


class AuthorizationError(Exception):
    """Raised when a principal cannot access a requested target."""

    def __init__(self, http_status: int, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.http_status = http_status
        self.reason = reason  # one of CONNECTION_ATTEMPT_REASONS for §14 metric
        self.detail = detail


def authorize_target(principal: Principal, target_id: str, target_registry) -> Any:
    """Confirm ``principal`` may use ``target_id`` and return the resolved
    ``TargetConfig``.

    Both directions of the allowlist must agree:
      * the principal's ``allowed_target_ids`` lists ``target_id``;
      * the target's ``allowed_caller_scopes`` lists ``principal.scope_name``.

    The legacy scope has no target access by design.

    Raises:
        AuthorizationError(http_status=403/404): on mismatch or unknown target.
    """
    if principal.is_legacy:
        raise AuthorizationError(
            http_status=403,
            reason="unauthorized",
            detail="legacy token cannot access registered targets",
        )
    target = target_registry.get(target_id)
    if target is None:
        raise AuthorizationError(
            http_status=404,
            reason="target_disabled",
            detail=f"target {target_id!r} is not registered or is disabled",
        )
    if not principal.allows_target(target_id):
        raise AuthorizationError(
            http_status=403,
            reason="unauthorized",
            detail=f"caller scope {principal.scope_name!r} is not allowlisted for {target_id!r}",
        )
    if principal.scope_name not in target.allowed_caller_scopes:
        raise AuthorizationError(
            http_status=403,
            reason="unauthorized",
            detail=f"target {target_id!r} does not allow caller scope {principal.scope_name!r}",
        )
    return target
