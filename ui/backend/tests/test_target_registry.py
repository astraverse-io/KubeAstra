"""Tests for the target registry loader.

Covers the validation rules in §7.1 of the remote-diagnostics plan: malformed
entries, duplicates, path-traversal in credential_path, unknown scopes, missing
identity, and disabled targets.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from target_registry import (
    DEFAULT_CREDENTIAL_ROOT,
    RegistryError,
    TargetConfig,
    TargetRegistry,
    load_registry,
)


_VALID_UID = "1c43dff0-0000-0000-0000-000000000000"


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "targets.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def _valid_entry(target_id: str = "qa17", *, credential_root: str = DEFAULT_CREDENTIAL_ROOT) -> str:
    return textwrap.dedent(
        f"""\
        targets:
          {target_id}:
            enabled: true
            display_name: QA 17
            environment_group: qa
            connection:
              type: ssh
              host: 10.40.17.10
              port: 22
              username: k8s-diagnostics
              known_hosts_alias: {target_id}-control-plane
              credential_path: {credential_root}/{target_id}
            expected_kube_system_uid: {_VALID_UID}
            diagnostic_scopes_allowed: [kubernetes]
            allowed_caller_scopes: [qa-ansible]
        """
    )


def test_valid_target_loads(tmp_path):
    p = _write(tmp_path, _valid_entry())
    registry = load_registry(p)
    assert len(registry) == 1
    cfg = registry.get("qa17")
    assert isinstance(cfg, TargetConfig)
    assert cfg.target_id == "qa17"
    assert cfg.host == "10.40.17.10"
    assert cfg.port == 22
    assert cfg.username == "k8s-diagnostics"
    assert cfg.diagnostic_scopes_allowed == frozenset({"kubernetes"})
    assert cfg.allowed_caller_scopes == frozenset({"qa-ansible"})
    assert cfg.expected_kube_system_uid == _VALID_UID


def test_missing_file_yields_empty_registry(tmp_path):
    registry = load_registry(tmp_path / "does-not-exist.yaml")
    assert isinstance(registry, TargetRegistry)
    assert len(registry) == 0


def test_disabled_target_is_dropped(tmp_path):
    body = _valid_entry().replace("enabled: true", "enabled: false")
    p = _write(tmp_path, body)
    registry = load_registry(p)
    assert "qa17" not in registry
    assert len(registry) == 0


def test_duplicate_target_id_fails(tmp_path):
    """Plan §7.1: duplicate target_id must be rejected, not silently
    overwritten. The custom SafeLoader raises on duplicate mapping keys."""
    body = textwrap.dedent(
        f"""\
        targets:
          qa17:
            enabled: true
            environment_group: qa
            connection:
              type: ssh
              host: 10.40.17.10
              port: 22
              username: k8s-diagnostics
              known_hosts_alias: a
              credential_path: {DEFAULT_CREDENTIAL_ROOT}/qa17
            expected_kube_system_uid: {_VALID_UID}
            diagnostic_scopes_allowed: [kubernetes]
            allowed_caller_scopes: [qa-ansible]
          qa17:
            enabled: true
            environment_group: qa
            connection:
              type: ssh
              host: 10.40.17.11
              port: 22
              username: k8s-diagnostics
              known_hosts_alias: a
              credential_path: {DEFAULT_CREDENTIAL_ROOT}/qa17
            expected_kube_system_uid: {_VALID_UID}
            diagnostic_scopes_allowed: [kubernetes]
            allowed_caller_scopes: [qa-ansible]
        """
    )
    p = _write(tmp_path, body)
    with pytest.raises(RegistryError, match=r"duplicate key"):
        load_registry(p)


def test_duplicate_nested_key_also_fails(tmp_path):
    """A duplicate inside a target's connection block must also be rejected
    so we never silently accept a shadowed host/credential field."""
    body = textwrap.dedent(
        f"""\
        targets:
          qa17:
            enabled: true
            environment_group: qa
            connection:
              type: ssh
              host: 10.40.17.10
              host: 10.40.17.99
              port: 22
              username: k8s-diagnostics
              known_hosts_alias: a
              credential_path: {DEFAULT_CREDENTIAL_ROOT}/qa17
            expected_kube_system_uid: {_VALID_UID}
            diagnostic_scopes_allowed: [kubernetes]
            allowed_caller_scopes: [qa-ansible]
        """
    )
    p = _write(tmp_path, body)
    with pytest.raises(RegistryError, match=r"duplicate key"):
        load_registry(p)


def test_credential_path_outside_root_fails(tmp_path):
    body = _valid_entry().replace(
        f"credential_path: {DEFAULT_CREDENTIAL_ROOT}/qa17",
        "credential_path: /etc/passwd",
    )
    p = _write(tmp_path, body)
    with pytest.raises(RegistryError, match="outside the credential root"):
        load_registry(p)


def test_credential_path_traversal_fails(tmp_path):
    body = _valid_entry().replace(
        f"credential_path: {DEFAULT_CREDENTIAL_ROOT}/qa17",
        f"credential_path: {DEFAULT_CREDENTIAL_ROOT}/../etc/shadow",
    )
    p = _write(tmp_path, body)
    with pytest.raises(RegistryError, match="outside the credential root"):
        load_registry(p)


def test_invalid_target_id_fails(tmp_path):
    body = _valid_entry("QA17")  # uppercase rejected
    p = _write(tmp_path, body)
    with pytest.raises(RegistryError, match="target_id"):
        load_registry(p)


def test_invalid_host_fails(tmp_path):
    body = _valid_entry().replace("host: 10.40.17.10", "host: not a valid host")
    p = _write(tmp_path, body)
    with pytest.raises(RegistryError, match="host"):
        load_registry(p)


def test_invalid_port_fails(tmp_path):
    body = _valid_entry().replace("port: 22", "port: 70000")
    p = _write(tmp_path, body)
    with pytest.raises(RegistryError, match="port"):
        load_registry(p)


def test_unknown_diagnostic_scope_fails(tmp_path):
    body = _valid_entry().replace(
        "diagnostic_scopes_allowed: [kubernetes]",
        "diagnostic_scopes_allowed: [kubernetes, host]",
    )
    p = _write(tmp_path, body)
    with pytest.raises(RegistryError, match="diagnostic_scopes_allowed"):
        load_registry(p)


def test_missing_expected_uid_fails(tmp_path):
    body = _valid_entry().replace(f"expected_kube_system_uid: {_VALID_UID}", "")
    p = _write(tmp_path, body)
    with pytest.raises(RegistryError, match="expected_kube_system_uid"):
        load_registry(p)


def test_malformed_uid_fails(tmp_path):
    body = _valid_entry().replace(_VALID_UID, "not-a-uuid")
    p = _write(tmp_path, body)
    with pytest.raises(RegistryError, match="UUID"):
        load_registry(p)


def test_non_ssh_connection_type_fails(tmp_path):
    body = _valid_entry().replace("type: ssh", "type: http")
    p = _write(tmp_path, body)
    with pytest.raises(RegistryError, match="connection.type"):
        load_registry(p)


def test_invalid_yaml_fails(tmp_path):
    p = _write(tmp_path, "::: this is not yaml :::")
    with pytest.raises(RegistryError, match="YAML"):
        load_registry(p)


def test_root_must_be_mapping(tmp_path):
    p = _write(tmp_path, "- item1\n- item2\n")
    with pytest.raises(RegistryError, match="root must be a mapping"):
        load_registry(p)


def test_empty_file_yields_empty_registry(tmp_path):
    p = _write(tmp_path, "")
    registry = load_registry(p)
    assert len(registry) == 0


def test_targets_block_required(tmp_path):
    p = _write(tmp_path, "something_else: {}\n")
    with pytest.raises(RegistryError, match="targets"):
        load_registry(p)


def test_registry_targets_view_is_read_only(tmp_path):
    """P2.1: frozen=True alone is not enough — the underlying mapping must
    also be immutable so a caller cannot smuggle in or remove targets."""
    p = _write(tmp_path, _valid_entry())
    registry = load_registry(p)
    # Cannot reassign the attribute (frozen=True).
    with pytest.raises((AttributeError, TypeError)):
        registry.targets = {}  # type: ignore[misc]
    # Cannot mutate the mapping itself (MappingProxyType).
    with pytest.raises(TypeError):
        registry.targets["evil"] = "anything"  # type: ignore[index]
    with pytest.raises(TypeError):
        del registry.targets["qa17"]  # type: ignore[arg-type]
