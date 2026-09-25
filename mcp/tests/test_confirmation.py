"""Tests for confirmation tokens (blueprint §2).

Covers all 7 acceptance criteria:
  1. dry_run=True returns success + dry_run flag + preview + confirmation_token.
  2. Reusing the same token a second time fails with token_error="invalid_token".
  3. Token issued for delete_pod(ns=prod, pod=api-1) rejected when submitted
     for delete_pod(ns=prod, pod=api-2) with token_error="target_mismatch".
  4. Token for delete_pod rejected when submitted to scale_deployment with
     token_error="operation_mismatch".
  5. Token with 1s TTL: after 1.2s, consume returns "expired" (or
     "invalid_token" after purge).
  6. With REQUIRE_DESTRUCTIVE_CONFIRMATION=false, confirm=True without a
     token still executes (back-compat).
  7. For apply_patch, the patch body must be hashed into the fingerprint —
     a token for patch A must reject patch B even on the same resource.
"""

import os
import sys
import time
from unittest.mock import patch

import pytest

# Ensure mcp/ is on the path
MCP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)


@pytest.fixture(autouse=True)
def reset_token_store():
    """Clear the module-level token store between tests."""
    from services.confirmation import token_store
    token_store._clear_for_tests()
    yield
    token_store._clear_for_tests()


@pytest.fixture
def strict_mode(monkeypatch):
    """Enable REQUIRE_DESTRUCTIVE_CONFIRMATION + ENABLE_RECOVERY_OPERATIONS.

    Mutates the existing settings singleton in place so modules that
    captured `from config.settings import settings` at import time see
    the change (which monkeypatch's setenv + cache_clear would not affect).
    """
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "require_destructive_confirmation", True)
    monkeypatch.setattr(_settings, "enable_recovery_operations", True)
    monkeypatch.setattr(_settings, "confirmation_token_ttl_seconds", 60)
    yield


@pytest.fixture
def legacy_mode(monkeypatch):
    """Disable strict confirmation — confirm=True alone should still work."""
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "require_destructive_confirmation", False)
    monkeypatch.setattr(_settings, "enable_recovery_operations", True)
    yield


# ── Fingerprint ──────────────────────────────────────────────────────────────

class TestFingerprint:

    def test_same_inputs_same_fingerprint(self):
        from services.confirmation import fingerprint
        fp1 = fingerprint("delete_pod", namespace="prod", pod_name="api-1")
        fp2 = fingerprint("delete_pod", namespace="prod", pod_name="api-1")
        assert fp1 == fp2

    def test_different_operation_different_fingerprint(self):
        from services.confirmation import fingerprint
        a = fingerprint("delete_pod", namespace="prod", pod_name="api-1")
        b = fingerprint("scale_deployment", namespace="prod", pod_name="api-1")
        assert a != b

    def test_different_target_different_fingerprint(self):
        from services.confirmation import fingerprint
        a = fingerprint("delete_pod", namespace="prod", pod_name="api-1")
        b = fingerprint("delete_pod", namespace="prod", pod_name="api-2")
        assert a != b

    def test_kwarg_order_does_not_matter(self):
        from services.confirmation import fingerprint
        a = fingerprint("delete_pod", namespace="prod", pod_name="api-1")
        b = fingerprint("delete_pod", pod_name="api-1", namespace="prod")
        assert a == b


# ── TokenStore primitives ────────────────────────────────────────────────────

class TestTokenStore:

    def test_issue_returns_unique_tokens(self):
        from services.confirmation import token_store
        r1 = token_store.issue("delete_pod", "fp1", ttl_seconds=60)
        r2 = token_store.issue("delete_pod", "fp1", ttl_seconds=60)
        assert r1.token != r2.token

    def test_consume_success_returns_record(self):
        from services.confirmation import token_store
        record = token_store.issue("delete_pod", "fp1", ttl_seconds=60)
        consumed, reason = token_store.consume(record.token, "delete_pod", "fp1")
        assert consumed is not None
        assert reason is None
        assert consumed.token == record.token

    def test_consume_is_single_use(self):
        """Acceptance criterion #2 — reuse fails."""
        from services.confirmation import token_store
        record = token_store.issue("delete_pod", "fp1", ttl_seconds=60)
        first, _ = token_store.consume(record.token, "delete_pod", "fp1")
        assert first is not None
        # Second consume must fail
        second, reason = token_store.consume(record.token, "delete_pod", "fp1")
        assert second is None
        assert reason == "invalid_token"

    def test_consume_missing_token(self):
        from services.confirmation import token_store
        _, reason = token_store.consume("", "delete_pod", "fp1")
        assert reason == "missing_token"

    def test_consume_unknown_token(self):
        from services.confirmation import token_store
        _, reason = token_store.consume("does-not-exist", "delete_pod", "fp1")
        assert reason == "invalid_token"

    def test_consume_operation_mismatch(self):
        """Acceptance criterion #4 — token for delete_pod rejected for scale."""
        from services.confirmation import token_store
        record = token_store.issue("delete_pod", "fp1", ttl_seconds=60)
        consumed, reason = token_store.consume(record.token, "scale_deployment", "fp1")
        assert consumed is None
        assert reason == "operation_mismatch"
        # Token should NOT be consumed when operation mismatches —
        # try with the right op, it should still succeed.
        consumed2, reason2 = token_store.consume(record.token, "delete_pod", "fp1")
        assert consumed2 is not None
        assert reason2 is None

    def test_consume_target_mismatch(self):
        """Acceptance criterion #3 — wrong target rejected."""
        from services.confirmation import token_store
        record = token_store.issue("delete_pod", "fp-pod-A", ttl_seconds=60)
        consumed, reason = token_store.consume(record.token, "delete_pod", "fp-pod-B")
        assert consumed is None
        assert reason == "target_mismatch"

    def test_consume_expired(self):
        """Acceptance criterion #5 — expired token rejected."""
        from services.confirmation import token_store
        record = token_store.issue("delete_pod", "fp1", ttl_seconds=1)
        # Sleep past TTL
        time.sleep(1.2)
        consumed, reason = token_store.consume(record.token, "delete_pod", "fp1")
        assert consumed is None
        assert reason in ("expired", "invalid_token")


# ── Wrapper integration ──────────────────────────────────────────────────────

class TestDeletePodTwoStep:

    def test_dry_run_returns_preview_and_token(self, strict_mode):
        """Acceptance criterion #1."""
        from k8s.wrappers import delete_pod
        # Mock the kubectl runner so we don't need a real cluster
        with patch("k8s.wrappers.get_runner") as mock_runner:
            mock_result = type("R", (), {
                "stdout": "pod/api-1 deleted (server dry run)",
                "stderr": "", "truncated": False,
            })()
            mock_runner.return_value.run.return_value = mock_result

            result = delete_pod("prod", "api-1", dry_run=True)

        assert result["success"] is True
        assert result["dry_run"] is True
        assert "preview" in result and result["preview"]
        assert "confirmation_token" in result
        assert len(result["confirmation_token"]) >= 24
        assert result["confirmation_required"] is True

    def test_execute_with_valid_token_succeeds(self, strict_mode):
        from k8s.wrappers import delete_pod
        with patch("k8s.wrappers.get_runner") as mock_runner:
            mock_result = type("R", (), {
                "stdout": "pod/api-1 deleted",
                "stderr": "", "truncated": False,
            })()
            mock_runner.return_value.run.return_value = mock_result

            # Step 1
            dry = delete_pod("prod", "api-1", dry_run=True)
            token = dry["confirmation_token"]

            # Step 2
            result = delete_pod("prod", "api-1", confirm=True,
                                confirmation_token=token)

        assert result["success"] is True
        assert "deleted" in result["message"]

    def test_execute_without_token_rejected(self, strict_mode):
        from k8s.wrappers import delete_pod
        # No mock needed — should error before kubectl runs
        result = delete_pod("prod", "api-1", confirm=True)
        assert result["success"] is False
        assert result["token_error"] == "missing_token"

    def test_execute_with_token_for_different_pod_rejected(self, strict_mode):
        """Acceptance criterion #3 — wrong target rejected.

        Internal rejection reason is `target_mismatch` (in audit log), but
        the external response coarse-grains to `invalid_token` to avoid
        leaking which check failed.
        """
        from k8s.wrappers import delete_pod
        with patch("k8s.wrappers.get_runner") as mock_runner:
            mock_result = type("R", (), {
                "stdout": "preview", "stderr": "", "truncated": False,
            })()
            mock_runner.return_value.run.return_value = mock_result

            dry = delete_pod("prod", "api-1", dry_run=True)
            token = dry["confirmation_token"]

            # Try to use the token for a different pod
            result = delete_pod("prod", "api-2", confirm=True,
                                confirmation_token=token)

        assert result["success"] is False
        # External response: coarse-grained for security.
        assert result["token_error"] == "invalid_token"

    def test_token_for_delete_rejected_by_scale(self, strict_mode):
        """Acceptance criterion #4 — operation mismatch rejected.

        Internal rejection reason is `operation_mismatch` (audit log),
        external response coarse-grains to `invalid_token`.
        """
        from k8s.wrappers import delete_pod, scale_deployment
        with patch("k8s.wrappers.get_runner") as mock_runner:
            mock_result = type("R", (), {
                "stdout": "preview", "stderr": "", "truncated": False,
            })()
            mock_runner.return_value.run.return_value = mock_result

            dry = delete_pod("prod", "api-1", dry_run=True)
            token = dry["confirmation_token"]

            # Try to use the token for a scale operation
            result = scale_deployment("prod", "api-1", 3, confirm=True,
                                       confirmation_token=token)

        assert result["success"] is False
        # External response: coarse-grained for security.
        assert result["token_error"] == "invalid_token"

    def test_token_is_single_use(self, strict_mode):
        """Acceptance criterion #2 — second attempt fails."""
        from k8s.wrappers import delete_pod
        with patch("k8s.wrappers.get_runner") as mock_runner:
            mock_result = type("R", (), {
                "stdout": "ok", "stderr": "", "truncated": False,
            })()
            mock_runner.return_value.run.return_value = mock_result

            dry = delete_pod("prod", "api-1", dry_run=True)
            token = dry["confirmation_token"]

            # First execute — succeeds
            first = delete_pod("prod", "api-1", confirm=True,
                               confirmation_token=token)
            assert first["success"] is True

            # Second execute with same token — fails
            second = delete_pod("prod", "api-1", confirm=True,
                                confirmation_token=token)
            assert second["success"] is False
            assert second["token_error"] == "invalid_token"


class TestApplyPatchBodyHash:
    """Acceptance criterion #7 — patch body MUST be in the fingerprint."""

    def test_token_for_patch_A_rejects_patch_B_same_resource(self, strict_mode):
        """Patch swap rejected — different patch body produces different
        fingerprint even on the same resource. External response coarse-
        grains to `invalid_token`."""
        from k8s.wrappers import apply_patch
        with patch("k8s.wrappers.get_runner") as mock_runner:
            mock_result = type("R", (), {
                "stdout": "preview", "stderr": "", "truncated": False,
            })()
            mock_runner.return_value.run.return_value = mock_result

            patch_a = '{"spec":{"replicas":3}}'
            patch_b = '{"spec":{"replicas":0}}'   # different intent

            dry = apply_patch("prod", "deployment", "api", patch_a, dry_run=True)
            token = dry["confirmation_token"]

            # Try to use the token to apply a DIFFERENT patch to the same resource
            result = apply_patch("prod", "deployment", "api", patch_b,
                                  confirm=True, confirmation_token=token)

        assert result["success"] is False
        # External response: coarse-grained.
        assert result["token_error"] == "invalid_token"


class TestLegacyMode:
    """Acceptance criterion #6 — back-compat when strict mode is off."""

    def test_confirm_true_alone_executes_in_legacy_mode(self, legacy_mode):
        from k8s.wrappers import delete_pod
        with patch("k8s.wrappers.get_runner") as mock_runner:
            mock_result = type("R", (), {
                "stdout": "pod/api-1 deleted",
                "stderr": "", "truncated": False,
            })()
            mock_runner.return_value.run.return_value = mock_result

            # In legacy mode, confirm=True without a token still executes.
            result = delete_pod("prod", "api-1", confirm=True)

        assert result["success"] is True

    def test_dry_run_in_legacy_mode_returns_preview_no_token(self, legacy_mode):
        from k8s.wrappers import delete_pod
        with patch("k8s.wrappers.get_runner") as mock_runner:
            mock_result = type("R", (), {
                "stdout": "preview", "stderr": "", "truncated": False,
            })()
            mock_runner.return_value.run.return_value = mock_result

            result = delete_pod("prod", "api-1", dry_run=True)

        assert result["success"] is True
        assert result["dry_run"] is True
        # Token issuance is gated on strict mode
        assert "confirmation_token" not in result
        assert result["confirmation_required"] is False
