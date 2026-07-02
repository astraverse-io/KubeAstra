"""Server-managed registry of SSH-reachable diagnostic targets.

Non-secret target metadata (host, port, username, expected cluster identity,
allowed caller scopes) is loaded from a YAML ConfigMap mount. Secret material
(passwords, private keys) lives in a separate read-only mount referenced by
``credential_path`` and is never read here — the SSH runner loads it on demand.

The registry is parsed and validated once at startup. Invalid entries cause
the whole load to fail closed; one bad entry does not partially populate the
registry. Targets are exposed as immutable ``TargetConfig`` instances.

Used by /api/v1/agent/invoke (Phase 1 of the Ansible remote diagnostics plan,
behind ``backend.agentApi.remoteDiagnostics.enabled``).
"""

from __future__ import annotations

import ipaddress
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys.

    PyYAML's default ``construct_mapping`` silently collapses duplicates (last
    wins). For a target registry, that means a typo could shadow another
    environment's host without warning. Override to raise on the first
    duplicate so configuration mistakes surface at load time.
    """


def _construct_mapping_no_duplicates(loader: yaml.SafeLoader, node: yaml.MappingNode):
    if not isinstance(node, yaml.MappingNode):  # pragma: no cover — defensive
        raise yaml.constructor.ConstructorError(
            None, None, f"expected a mapping node, but found {node.id}", node.start_mark
        )
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key: {key!r}",
                key_node.start_mark,
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_no_duplicates,
)


_TARGET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SCOPE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*"
    r"[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$"
)
_VALID_SCOPES = frozenset({"kubernetes"})
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

DEFAULT_CREDENTIAL_ROOT = "/var/run/agent-target-credentials"


class RegistryError(ValueError):
    """Raised when the target registry file is malformed."""


@dataclass(frozen=True, slots=True)
class TargetConfig:
    """Immutable resolved target metadata.

    Credentials are referenced by ``credential_path`` but never read here.
    """

    target_id: str
    display_name: str
    environment_group: str
    host: str
    port: int
    username: str
    known_hosts_alias: str
    credential_path: str
    expected_kube_system_uid: str
    diagnostic_scopes_allowed: frozenset[str]
    allowed_caller_scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class TargetRegistry:
    """Immutable collection of targets keyed by target_id.

    ``frozen=True`` prevents replacing the ``targets`` attribute; wrapping the
    parsed dict in ``MappingProxyType`` additionally prevents mutation of the
    contents (no item assignment, no del, no clear). The loader is the only
    code path that can produce a registry, so callers cannot inject targets at
    runtime.
    """

    targets: Mapping[str, TargetConfig]

    def __post_init__(self) -> None:
        # If the caller passed a plain dict, wrap it. If they already passed a
        # MappingProxyType (or another read-only view), keep it as-is.
        if not isinstance(self.targets, MappingProxyType):
            object.__setattr__(self, "targets", MappingProxyType(dict(self.targets)))

    def get(self, target_id: str) -> TargetConfig | None:
        return self.targets.get(target_id)

    def __contains__(self, target_id: str) -> bool:
        return target_id in self.targets

    def __len__(self) -> int:
        return len(self.targets)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RegistryError(message)


def _validate_host(value: Any, target_id: str) -> str:
    _require(isinstance(value, str) and value, f"{target_id}: host must be a non-empty string")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if _HOSTNAME_RE.match(value):
        return value
    raise RegistryError(f"{target_id}: host {value!r} is neither a valid IP nor a DNS name")


def _validate_port(value: Any, target_id: str) -> int:
    _require(
        isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535,
        f"{target_id}: port must be an integer in [1, 65535]",
    )
    return int(value)


def _validate_credential_path(value: Any, target_id: str, credential_root: str) -> str:
    _require(isinstance(value, str) and value, f"{target_id}: credential_path must be a non-empty string")
    resolved = Path(value).resolve()
    root_resolved = Path(credential_root).resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise RegistryError(
            f"{target_id}: credential_path {value!r} is outside the credential root {credential_root!r}"
        ) from exc
    return str(resolved)


def _validate_uuid(value: Any, target_id: str) -> str:
    _require(
        isinstance(value, str) and _UUID_RE.match(value) is not None,
        f"{target_id}: expected_kube_system_uid must be a UUID string",
    )
    return value.lower()


def _validate_scope_set(value: Any, target_id: str, field: str) -> frozenset[str]:
    _require(isinstance(value, list) and value, f"{target_id}: {field} must be a non-empty list")
    out: set[str] = set()
    for item in value:
        _require(
            isinstance(item, str) and item,
            f"{target_id}: {field} entries must be non-empty strings",
        )
        out.add(item)
    if field == "diagnostic_scopes_allowed":
        unknown = out - _VALID_SCOPES
        _require(
            not unknown,
            f"{target_id}: unknown diagnostic_scopes_allowed entries: {sorted(unknown)!r}",
        )
    if field == "allowed_caller_scopes":
        for name in out:
            _require(
                _SCOPE_NAME_RE.match(name) is not None,
                f"{target_id}: allowed_caller_scopes entry {name!r} is malformed",
            )
    return frozenset(out)


def _parse_target(
    target_id: str,
    raw: Mapping[str, Any],
    credential_root: str,
) -> TargetConfig | None:
    """Parse one target entry. Returns None when ``enabled: false``."""
    _require(isinstance(raw, dict), f"{target_id}: entry must be a mapping")
    _require(
        _TARGET_ID_RE.match(target_id) is not None,
        f"{target_id}: target_id must match {_TARGET_ID_RE.pattern}",
    )
    if raw.get("enabled") is False:
        return None
    _require(raw.get("enabled", True) is True, f"{target_id}: enabled must be a boolean")

    display_name = raw.get("display_name") or target_id
    _require(isinstance(display_name, str), f"{target_id}: display_name must be a string")

    environment_group = raw.get("environment_group")
    _require(
        isinstance(environment_group, str) and environment_group,
        f"{target_id}: environment_group is required",
    )

    connection = raw.get("connection")
    _require(isinstance(connection, dict), f"{target_id}: connection block is required")
    _require(
        connection.get("type") == "ssh",
        f"{target_id}: connection.type must be 'ssh' in v1",
    )
    host = _validate_host(connection.get("host"), target_id)
    port = _validate_port(connection.get("port", 22), target_id)

    username = connection.get("username")
    _require(
        isinstance(username, str) and username and len(username) <= 32,
        f"{target_id}: connection.username must be a non-empty string up to 32 chars",
    )

    known_hosts_alias = connection.get("known_hosts_alias")
    _require(
        isinstance(known_hosts_alias, str) and known_hosts_alias,
        f"{target_id}: connection.known_hosts_alias is required",
    )

    credential_path = _validate_credential_path(
        connection.get("credential_path"), target_id, credential_root
    )

    expected_kube_system_uid = _validate_uuid(
        raw.get("expected_kube_system_uid"), target_id
    )

    diagnostic_scopes_allowed = _validate_scope_set(
        raw.get("diagnostic_scopes_allowed"), target_id, "diagnostic_scopes_allowed"
    )
    allowed_caller_scopes = _validate_scope_set(
        raw.get("allowed_caller_scopes"), target_id, "allowed_caller_scopes"
    )

    return TargetConfig(
        target_id=target_id,
        display_name=display_name,
        environment_group=environment_group,
        host=host,
        port=port,
        username=username,
        known_hosts_alias=known_hosts_alias,
        credential_path=credential_path,
        expected_kube_system_uid=expected_kube_system_uid,
        diagnostic_scopes_allowed=diagnostic_scopes_allowed,
        allowed_caller_scopes=allowed_caller_scopes,
    )


def load_registry(
    path: str | Path,
    *,
    credential_root: str = DEFAULT_CREDENTIAL_ROOT,
) -> TargetRegistry:
    """Parse and validate the entire registry file. All-or-nothing.

    Returns an empty registry when the file is missing — callers gate remote
    diagnostics on ``backend.agentApi.remoteDiagnostics.enabled`` anyway, so a
    missing file is not an error during the rollout window.

    Raises:
        RegistryError: when the file exists but contains invalid entries.
    """
    p = Path(path)
    if not p.exists():
        return TargetRegistry(targets={})
    try:
        raw = yaml.load(p.read_text(), Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise RegistryError(f"registry file {p} is not valid YAML: {exc}") from exc

    if raw is None:
        return TargetRegistry(targets={})
    _require(isinstance(raw, dict), "registry root must be a mapping")
    targets_block = raw.get("targets")
    _require(isinstance(targets_block, dict), "registry must have a 'targets' mapping")

    parsed: dict[str, TargetConfig] = {}
    for target_id, entry in targets_block.items():
        _require(isinstance(target_id, str), f"target keys must be strings (got {target_id!r})")
        if target_id in parsed:
            raise RegistryError(f"duplicate target_id: {target_id}")
        cfg = _parse_target(target_id, entry, credential_root)
        if cfg is not None:
            parsed[target_id] = cfg
    return TargetRegistry(targets=parsed)


_lock = threading.Lock()
_active: TargetRegistry | None = None


def set_active(registry: TargetRegistry) -> None:
    """Install the registry returned by ``load_registry`` for request handlers."""
    global _active
    with _lock:
        _active = registry


def get_active() -> TargetRegistry:
    """Return the installed registry, or an empty one if not yet loaded."""
    with _lock:
        return _active if _active is not None else TargetRegistry(targets={})
