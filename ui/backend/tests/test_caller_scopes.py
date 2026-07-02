"""Tests for the caller-scope registry and target authorization (Phase 2).

Covers plan §8 (Caller Authentication and Authorization): scoped tokens
match only their own targets; per-scope rotation is independent; the legacy
unscoped token has no target access; authorization requires both the
caller's and the target's allowlists to intersect.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

import caller_scopes
from caller_scopes import (
    AuthorizationError,
    CallerScope,
    LEGACY_SCOPE_NAME,
    Principal,
    ScopeRegistry,
    ScopeRegistryError,
    authorize_target,
    load_registry,
)


# ── Test fixtures ────────────────────────────────────────────────────────────


def _write_tokens(tmp_path: Path, scope: str, current: str, previous: str | None = None) -> Path:
    """Lay out the file structure that the Kubernetes Secret projection
    would create at /var/run/agent-caller-tokens/<scope>/."""
    scope_dir = tmp_path / "tokens" / scope
    scope_dir.mkdir(parents=True)
    (scope_dir / "current").write_text(current)
    if previous is not None:
        (scope_dir / "previous").write_text(previous)
    return scope_dir


def _write_scopes_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "scopes.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def _basic_qa_yaml(tmp_path: Path) -> str:
    qa_dir = _write_tokens(tmp_path, "qa-ansible", "QA_CURRENT", "QA_PREVIOUS")
    return f"""\
        callerScopes:
          qa-ansible:
            current_token_path: {qa_dir / "current"}
            previous_token_path: {qa_dir / "previous"}
            allowed_target_ids: [qa01, qa17]
        """


class _FakeTarget:
    """Minimal stand-in for TargetConfig — we only use ``allowed_caller_scopes``."""

    def __init__(self, target_id: str, allowed_caller_scopes: set[str]) -> None:
        self.target_id = target_id
        self.allowed_caller_scopes = frozenset(allowed_caller_scopes)


class _FakeRegistry:
    def __init__(self, targets: dict[str, _FakeTarget]) -> None:
        self._targets = targets

    def get(self, target_id: str) -> _FakeTarget | None:
        return self._targets.get(target_id)


# ── Loader / registry validation ─────────────────────────────────────────────


def test_missing_file_yields_empty_registry(tmp_path):
    registry = load_registry(tmp_path / "absent.yaml")
    assert len(registry) == 0
    assert registry.legacy is None


def test_legacy_token_registered_when_provided(tmp_path):
    registry = load_registry(
        tmp_path / "absent.yaml",
        legacy_current_token="LEGACY",
        legacy_previous_token="LEGACY_PREV",
    )
    assert registry.legacy is not None
    assert registry.legacy.is_legacy
    assert registry.legacy.allowed_target_ids == frozenset()


def test_valid_scope_loads(tmp_path):
    p = _write_scopes_yaml(tmp_path, _basic_qa_yaml(tmp_path))
    registry = load_registry(p, token_mount_root=str(tmp_path / "tokens"))
    assert len(registry) == 1
    scope = registry.get("qa-ansible")
    assert scope is not None
    assert scope.current_token == "QA_CURRENT"
    assert scope.previous_token == "QA_PREVIOUS"
    assert scope.allowed_target_ids == frozenset({"qa01", "qa17"})


def test_previous_token_is_optional(tmp_path):
    qa_dir = _write_tokens(tmp_path, "qa-ansible", "QA_CURRENT")  # no previous
    body = f"""\
        callerScopes:
          qa-ansible:
            current_token_path: {qa_dir / "current"}
            allowed_target_ids: [qa01]
        """
    p = _write_scopes_yaml(tmp_path, body)
    scope = load_registry(p, token_mount_root=str(tmp_path / "tokens")).get("qa-ansible")
    assert scope is not None
    assert scope.previous_token is None


def test_token_path_outside_mount_root_fails(tmp_path):
    body = f"""\
        callerScopes:
          qa-ansible:
            current_token_path: /etc/passwd
            allowed_target_ids: [qa01]
        """
    p = _write_scopes_yaml(tmp_path, body)
    with pytest.raises(ScopeRegistryError, match="outside the token mount root"):
        load_registry(p, token_mount_root=str(tmp_path / "tokens"))


def test_token_path_traversal_fails(tmp_path):
    body = f"""\
        callerScopes:
          qa-ansible:
            current_token_path: {tmp_path / "tokens"}/../../etc/shadow
            allowed_target_ids: [qa01]
        """
    p = _write_scopes_yaml(tmp_path, body)
    with pytest.raises(ScopeRegistryError, match="outside the token mount root"):
        load_registry(p, token_mount_root=str(tmp_path / "tokens"))


def test_missing_token_file_fails(tmp_path):
    body = f"""\
        callerScopes:
          qa-ansible:
            current_token_path: {tmp_path / "tokens"}/qa-ansible/current
            allowed_target_ids: [qa01]
        """
    (tmp_path / "tokens" / "qa-ansible").mkdir(parents=True)
    p = _write_scopes_yaml(tmp_path, body)
    with pytest.raises(ScopeRegistryError, match="token file does not exist"):
        load_registry(p, token_mount_root=str(tmp_path / "tokens"))


def test_empty_token_file_fails(tmp_path):
    qa_dir = _write_tokens(tmp_path, "qa-ansible", "")
    body = f"""\
        callerScopes:
          qa-ansible:
            current_token_path: {qa_dir / "current"}
            allowed_target_ids: [qa01]
        """
    p = _write_scopes_yaml(tmp_path, body)
    with pytest.raises(ScopeRegistryError, match="empty"):
        load_registry(p, token_mount_root=str(tmp_path / "tokens"))


def test_reserved_scope_name_rejected(tmp_path):
    """The LEGACY_SCOPE_NAME sentinel uses underscores which the scope-name
    regex already rejects, so this entry fails at the regex check. The
    explicit reserved-name guard in _parse_scope is defense-in-depth in case
    the regex is ever loosened."""
    qa_dir = _write_tokens(tmp_path, "x", "TOKEN")
    body = f"""\
        callerScopes:
          __legacy_unscoped__:
            current_token_path: {qa_dir / "current"}
            allowed_target_ids: [qa01]
        """
    p = _write_scopes_yaml(tmp_path, body)
    with pytest.raises(ScopeRegistryError, match=r"scope name|reserved"):
        load_registry(p, token_mount_root=str(tmp_path / "tokens"))


def test_malformed_scope_name_rejected(tmp_path):
    qa_dir = _write_tokens(tmp_path, "x", "TOKEN")
    body = f"""\
        callerScopes:
          QA-ANSIBLE:
            current_token_path: {qa_dir / "current"}
            allowed_target_ids: [qa01]
        """
    p = _write_scopes_yaml(tmp_path, body)
    with pytest.raises(ScopeRegistryError, match="scope name"):
        load_registry(p, token_mount_root=str(tmp_path / "tokens"))


def test_empty_allowed_target_ids_rejected(tmp_path):
    qa_dir = _write_tokens(tmp_path, "qa-ansible", "TOKEN")
    body = f"""\
        callerScopes:
          qa-ansible:
            current_token_path: {qa_dir / "current"}
            allowed_target_ids: []
        """
    p = _write_scopes_yaml(tmp_path, body)
    with pytest.raises(ScopeRegistryError, match="allowed_target_ids"):
        load_registry(p, token_mount_root=str(tmp_path / "tokens"))


def test_duplicate_scope_yaml_rejected(tmp_path):
    qa_dir = _write_tokens(tmp_path, "qa-ansible", "TOKEN")
    body = f"""\
        callerScopes:
          qa-ansible:
            current_token_path: {qa_dir / "current"}
            allowed_target_ids: [qa01]
          qa-ansible:
            current_token_path: {qa_dir / "current"}
            allowed_target_ids: [qa02]
        """
    p = _write_scopes_yaml(tmp_path, body)
    with pytest.raises(ScopeRegistryError, match=r"duplicate key"):
        load_registry(p, token_mount_root=str(tmp_path / "tokens"))


def test_registry_scopes_view_is_read_only(tmp_path):
    p = _write_scopes_yaml(tmp_path, _basic_qa_yaml(tmp_path))
    registry = load_registry(p, token_mount_root=str(tmp_path / "tokens"))
    with pytest.raises(TypeError):
        registry.scopes["evil"] = "x"  # type: ignore[index]
    with pytest.raises((AttributeError, TypeError)):
        registry.scopes = {}  # type: ignore[misc]


# ── Token identification ─────────────────────────────────────────────────────


def test_current_token_identifies_scope(tmp_path):
    p = _write_scopes_yaml(tmp_path, _basic_qa_yaml(tmp_path))
    registry = load_registry(p, token_mount_root=str(tmp_path / "tokens"))
    principal = registry.identify_token("QA_CURRENT")
    assert principal is not None
    assert isinstance(principal, Principal)
    assert principal.scope_name == "qa-ansible"


def test_previous_token_identifies_scope_during_rotation(tmp_path):
    p = _write_scopes_yaml(tmp_path, _basic_qa_yaml(tmp_path))
    registry = load_registry(p, token_mount_root=str(tmp_path / "tokens"))
    principal = registry.identify_token("QA_PREVIOUS")
    assert principal is not None
    assert principal.scope_name == "qa-ansible"


def test_unknown_token_returns_none(tmp_path):
    p = _write_scopes_yaml(tmp_path, _basic_qa_yaml(tmp_path))
    registry = load_registry(p, token_mount_root=str(tmp_path / "tokens"))
    assert registry.identify_token("WRONG") is None


def test_empty_token_returns_none_without_throwing(tmp_path):
    p = _write_scopes_yaml(tmp_path, _basic_qa_yaml(tmp_path))
    registry = load_registry(p, token_mount_root=str(tmp_path / "tokens"))
    assert registry.identify_token("") is None


def test_legacy_token_identified_when_registered(tmp_path):
    registry = load_registry(
        tmp_path / "absent.yaml",
        legacy_current_token="LEGACY",
    )
    principal = registry.identify_token("LEGACY")
    assert principal is not None
    assert principal.is_legacy
    assert principal.scope_name == LEGACY_SCOPE_NAME


def test_per_scope_rotation_is_independent(tmp_path):
    """Plan §8: rotating qa-ansible's tokens does not affect staging-ansible."""
    qa_dir = _write_tokens(tmp_path, "qa-ansible", "QA_NEW", "QA_OLD")
    st_dir = _write_tokens(tmp_path, "staging-ansible", "STAGING_NEW", "STAGING_OLD")
    body = f"""\
        callerScopes:
          qa-ansible:
            current_token_path: {qa_dir / "current"}
            previous_token_path: {qa_dir / "previous"}
            allowed_target_ids: [qa01]
          staging-ansible:
            current_token_path: {st_dir / "current"}
            previous_token_path: {st_dir / "previous"}
            allowed_target_ids: [staging01]
        """
    p = _write_scopes_yaml(tmp_path, body)
    registry = load_registry(p, token_mount_root=str(tmp_path / "tokens"))
    # QA_OLD still works (previous), STAGING_NEW resolves to its own scope.
    assert registry.identify_token("QA_OLD").scope_name == "qa-ansible"
    assert registry.identify_token("STAGING_NEW").scope_name == "staging-ansible"
    # No cross-talk.
    assert registry.identify_token("STAGING_NEW").allowed_target_ids == frozenset({"staging01"})
    assert registry.identify_token("QA_NEW").allowed_target_ids == frozenset({"qa01"})


# ── Authorization ────────────────────────────────────────────────────────────


def _principal(name: str, allowed: set[str]) -> Principal:
    return Principal(
        scope_name=name,
        token_fingerprint="fp" + name[:6],
        allowed_target_ids=frozenset(allowed),
    )


def test_legacy_scope_cannot_access_any_target():
    legacy = _principal(LEGACY_SCOPE_NAME, set())
    targets = _FakeRegistry({"qa17": _FakeTarget("qa17", {"qa-ansible"})})
    with pytest.raises(AuthorizationError) as exc_info:
        authorize_target(legacy, "qa17", targets)
    assert exc_info.value.http_status == 403
    assert exc_info.value.reason == "unauthorized"


def test_scope_authorized_for_listed_target():
    principal = _principal("qa-ansible", {"qa17"})
    targets = _FakeRegistry({"qa17": _FakeTarget("qa17", {"qa-ansible"})})
    target = authorize_target(principal, "qa17", targets)
    assert target.target_id == "qa17"


def test_target_not_in_callers_allowlist_returns_403():
    principal = _principal("qa-ansible", {"qa01"})  # qa17 is not allowed
    targets = _FakeRegistry({"qa17": _FakeTarget("qa17", {"qa-ansible"})})
    with pytest.raises(AuthorizationError) as exc_info:
        authorize_target(principal, "qa17", targets)
    assert exc_info.value.http_status == 403
    assert exc_info.value.reason == "unauthorized"


def test_callers_scope_not_in_targets_allowlist_returns_403():
    """Plan §8: both directions of the allowlist must agree. A caller's
    allowlist permitting a target is not enough if the target doesn't also
    list the caller scope."""
    principal = _principal("qa-ansible", {"qa17"})
    targets = _FakeRegistry({"qa17": _FakeTarget("qa17", {"staging-ansible"})})
    with pytest.raises(AuthorizationError) as exc_info:
        authorize_target(principal, "qa17", targets)
    assert exc_info.value.http_status == 403


def test_unknown_target_returns_404():
    principal = _principal("qa-ansible", {"qa17"})
    targets = _FakeRegistry({})  # qa17 not registered
    with pytest.raises(AuthorizationError) as exc_info:
        authorize_target(principal, "qa17", targets)
    assert exc_info.value.http_status == 404
    assert exc_info.value.reason == "target_disabled"


# ── New regression tests for the five fixes ──────────────────────────────────


def test_duplicate_token_across_scopes_rejected(tmp_path):
    """Plan §8: tokens must be globally unique. A YAML where two scopes share
    a token value is a real misconfiguration — identify_token would otherwise
    return whichever scope iterates first, making authorization
    order-dependent."""
    qa_dir = _write_tokens(tmp_path, "qa-ansible", "SHARED_TOKEN")
    st_dir = _write_tokens(tmp_path, "staging-ansible", "SHARED_TOKEN")
    body = f"""\
        callerScopes:
          qa-ansible:
            current_token_path: {qa_dir / "current"}
            allowed_target_ids: [qa01]
          staging-ansible:
            current_token_path: {st_dir / "current"}
            allowed_target_ids: [staging01]
        """
    p = _write_scopes_yaml(tmp_path, body)
    with pytest.raises(ScopeRegistryError, match=r"duplicate token") as exc_info:
        load_registry(p, token_mount_root=str(tmp_path / "tokens"))
    # Error must mention scope names but NEVER the token value itself.
    assert "SHARED_TOKEN" not in str(exc_info.value)


def test_duplicate_token_between_scope_and_legacy_rejected(tmp_path):
    qa_dir = _write_tokens(tmp_path, "qa-ansible", "SAME_AS_LEGACY")
    body = f"""\
        callerScopes:
          qa-ansible:
            current_token_path: {qa_dir / "current"}
            allowed_target_ids: [qa01]
        """
    p = _write_scopes_yaml(tmp_path, body)
    with pytest.raises(ScopeRegistryError, match=r"duplicate token"):
        load_registry(
            p,
            token_mount_root=str(tmp_path / "tokens"),
            legacy_current_token="SAME_AS_LEGACY",
        )


def test_duplicate_current_and_previous_within_one_scope_rejected(tmp_path):
    """An operator accidentally setting the same value for current and
    previous would otherwise cause both compare_digest calls to match
    deterministically — flag it at load time."""
    qa_dir = _write_tokens(tmp_path, "qa-ansible", "SAME", "SAME")
    body = f"""\
        callerScopes:
          qa-ansible:
            current_token_path: {qa_dir / "current"}
            previous_token_path: {qa_dir / "previous"}
            allowed_target_ids: [qa01]
        """
    p = _write_scopes_yaml(tmp_path, body)
    with pytest.raises(ScopeRegistryError, match=r"duplicate token"):
        load_registry(p, token_mount_root=str(tmp_path / "tokens"))


def test_caller_scope_repr_does_not_expose_tokens():
    """P1.3: dataclass field(repr=False) must hide tokens so default logging
    or tracing serialization cannot leak them."""
    scope = CallerScope(
        name="qa-ansible",
        current_token="VERY_SECRET",
        previous_token="ALSO_SECRET",
        allowed_target_ids=frozenset({"qa17"}),
    )
    rendered = repr(scope)
    assert "VERY_SECRET" not in rendered
    assert "ALSO_SECRET" not in rendered
    assert "qa-ansible" in rendered  # non-secret fields still visible


def test_principal_carries_no_token_material():
    """P1.3: Principal is the sanitized post-auth type passed downstream."""
    principal = Principal(
        scope_name="qa-ansible",
        token_fingerprint="abc123",
        allowed_target_ids=frozenset({"qa17"}),
    )
    # Repr is safe by construction — there are no token fields to expose.
    assert "token_fingerprint" in repr(principal)
    # Token fingerprint is non-reversible: only the prefix is exposed.
    assert len(principal.token_fingerprint) <= 16


def test_previous_token_path_set_but_file_missing_fails(tmp_path):
    """P2.1: if previous_token_path appears in YAML, the file MUST exist.
    Optionality belongs to the YAML field, not the file behind it; a missing
    projected Secret key is a broken rotation rollout."""
    qa_dir = _write_tokens(tmp_path, "qa-ansible", "CURRENT")
    body = f"""\
        callerScopes:
          qa-ansible:
            current_token_path: {qa_dir / "current"}
            previous_token_path: {qa_dir / "previous"}
            allowed_target_ids: [qa01]
        """
    # previous file is intentionally not created
    p = _write_scopes_yaml(tmp_path, body)
    with pytest.raises(ScopeRegistryError, match=r"token file does not exist"):
        load_registry(p, token_mount_root=str(tmp_path / "tokens"))


def test_omitting_previous_token_path_is_fine(tmp_path):
    """Counterpart to the above: leaving previous_token_path out of the YAML
    entirely is fine and means 'no previous token configured'."""
    qa_dir = _write_tokens(tmp_path, "qa-ansible", "CURRENT")
    body = f"""\
        callerScopes:
          qa-ansible:
            current_token_path: {qa_dir / "current"}
            allowed_target_ids: [qa01]
        """
    p = _write_scopes_yaml(tmp_path, body)
    registry = load_registry(p, token_mount_root=str(tmp_path / "tokens"))
    scope = registry.get("qa-ansible")
    assert scope is not None
    assert scope.previous_token is None


def test_has_any_principal_true_for_scopes_only(tmp_path):
    """P2.2: scoped-only deployments need not configure a legacy token."""
    p = _write_scopes_yaml(tmp_path, _basic_qa_yaml(tmp_path))
    registry = load_registry(p, token_mount_root=str(tmp_path / "tokens"))
    assert registry.has_any_principal() is True
    assert registry.legacy is None


def test_has_any_principal_true_for_legacy_only(tmp_path):
    registry = load_registry(
        tmp_path / "absent.yaml",
        legacy_current_token="LEGACY",
    )
    assert registry.has_any_principal() is True


def test_has_any_principal_false_when_empty(tmp_path):
    registry = load_registry(tmp_path / "absent.yaml")
    assert registry.has_any_principal() is False


# ── Active-registry helpers ──────────────────────────────────────────────────


def test_set_and_get_active(tmp_path):
    p = _write_scopes_yaml(tmp_path, _basic_qa_yaml(tmp_path))
    registry = load_registry(p, token_mount_root=str(tmp_path / "tokens"))
    caller_scopes.set_active(registry)
    try:
        active = caller_scopes.get_active()
        assert "qa-ansible" in active.scopes
    finally:
        # Reset to avoid bleeding state into other tests in this module.
        caller_scopes._active = None  # noqa: SLF001
