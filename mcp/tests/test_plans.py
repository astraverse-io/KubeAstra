"""Tests for multi-step remediation plans (blueprint §3).

Covers all 7 acceptance criteria:
  1. build_plan rejects unknown tool with allowed-tools message.
  2. build_plan rejects step missing required arg.
  3. build_plan rejects empty steps list and lists > max_steps.
  4. Plan TTL — plan_store.get() returns None after expires_at.
  5. Race: two concurrent execute_step calls on the same step →
     exactly one wins, the other returns reason="in_progress".
  6. execute_step on a `done` step returns reason="already_executed".
  7. Each step requires its OWN confirmation_token (§2 contract).

Plus a handful of edge cases: missing token, plan_not_found, invalid_step.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from unittest.mock import patch

import pytest

MCP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_plan_store():
    from services.plans import plan_store
    plan_store._clear_for_tests()
    yield
    plan_store._clear_for_tests()


@pytest.fixture
def strict_mode(monkeypatch):
    """Enable destructive ops + strict confirmation so the wrappers actually
    validate tokens. plans.py invokes wrappers which require this.

    Always read the LIVE cached settings instance (not config.settings.settings)
    — other test modules may have called get_settings.cache_clear() which
    creates a new instance and orphans the module-level alias.
    """
    from config.settings import get_settings
    _settings = get_settings()
    monkeypatch.setattr(_settings, "require_destructive_confirmation", True)
    monkeypatch.setattr(_settings, "enable_recovery_operations", True)
    monkeypatch.setattr(_settings, "confirmation_token_ttl_seconds", 60)
    monkeypatch.setattr(_settings, "plan_ttl_minutes", 15)
    monkeypatch.setattr(_settings, "plan_max_steps", 25)
    yield


# ── Validation (acceptance #1, #2, #3) ──────────────────────────────────────

class TestBuildPlanValidation:

    def test_unknown_tool_rejected(self, strict_mode):
        from services.plans import build_plan
        with pytest.raises(ValueError) as excinfo:
            build_plan(
                issue="fix",
                steps_input=[{"tool": "kubectl_apply", "args": {}}],
            )
        msg = str(excinfo.value)
        assert "not allowed" in msg
        assert "Allowed tools" in msg

    def test_missing_required_arg_rejected(self, strict_mode):
        from services.plans import build_plan
        with pytest.raises(ValueError) as excinfo:
            build_plan(
                issue="fix",
                steps_input=[{
                    "tool": "delete_pod",
                    "args": {"namespace": "prod"},  # missing pod_name
                }],
            )
        msg = str(excinfo.value)
        assert "delete_pod" in msg
        assert "pod_name" in msg

    def test_empty_steps_rejected(self, strict_mode):
        from services.plans import build_plan
        with pytest.raises(ValueError) as excinfo:
            build_plan(issue="fix", steps_input=[])
        assert "at least one step" in str(excinfo.value)

    def test_too_many_steps_rejected(self, strict_mode):
        from services.plans import build_plan, ALLOWED_TOOLS, _REQUIRED_ARGS  # noqa
        # Build 26 valid steps — one over the cap.
        steps = [
            {"tool": "delete_pod", "args": {"namespace": "prod", "pod_name": f"p{i}"}}
            for i in range(26)
        ]
        with pytest.raises(ValueError) as excinfo:
            build_plan(issue="fix", steps_input=steps)
        assert "at most 25" in str(excinfo.value)

    def test_empty_issue_rejected(self, strict_mode):
        from services.plans import build_plan
        with pytest.raises(ValueError):
            build_plan(issue="", steps_input=[
                {"tool": "delete_pod", "args": {"namespace": "prod", "pod_name": "p"}}
            ])
        with pytest.raises(ValueError):
            build_plan(issue="   ", steps_input=[
                {"tool": "delete_pod", "args": {"namespace": "prod", "pod_name": "p"}}
            ])

    def test_valid_plan_stored(self, strict_mode):
        from services.plans import build_plan, plan_store
        plan = build_plan(
            issue="api crashlooping",
            steps_input=[
                {"tool": "scale_deployment",
                 "args": {"namespace": "prod", "deployment_name": "api", "replicas": 0},
                 "why": "drain"},
                {"tool": "rollout_restart",
                 "args": {"namespace": "prod", "deployment_name": "api"},
                 "why": "fresh start"},
            ],
        )
        assert plan.plan_id.startswith("plan_")
        assert len(plan.steps) == 2
        assert plan.steps[0].step == 1
        assert plan.steps[1].step == 2
        # All pending initially
        assert all(s.status == "pending" for s in plan.steps)
        # Retrievable
        retrieved = plan_store.get(plan.plan_id)
        assert retrieved is not None
        assert retrieved.issue == "api crashlooping"


# ── TTL (acceptance #4) ─────────────────────────────────────────────────────

class TestPlanTTL:

    def test_expired_plan_returns_none(self, strict_mode, monkeypatch):
        """Set TTL to a fraction of a second so we can wait it out."""
        from config.settings import get_settings
        _settings = get_settings()
        monkeypatch.setattr(_settings, "plan_ttl_minutes", 0)  # 0 minutes = expires_at = now

        from services.plans import build_plan, plan_store
        plan = build_plan(
            issue="fix",
            steps_input=[
                {"tool": "delete_pod", "args": {"namespace": "p", "pod_name": "x"}}
            ],
        )
        # With TTL=0, expires_at = now. Sleep briefly then retrieve.
        time.sleep(0.05)
        assert plan_store.get(plan.plan_id) is None


# ── Concurrency (acceptance #5 — the race fix) ──────────────────────────────

class TestExecuteStepRace:
    """Two concurrent execute_step calls on the same step must NOT both succeed.

    The blueprint flags this as a real race: without atomic CAS, both
    callers pass the status check, both try to run the destructive op,
    the resource gets clobbered twice.
    """

    def test_concurrent_execute_exactly_one_succeeds(self, strict_mode):
        from services.plans import build_plan, execute_step

        plan = build_plan(
            issue="r",
            steps_input=[
                {"tool": "delete_pod", "args": {"namespace": "p", "pod_name": "x"}},
            ],
        )

        # Mock the wrapper so it sleeps briefly inside the "running" window.
        # The second concurrent call must observe status=running and bail.
        from k8s import wrappers
        original_delete = wrappers.delete_pod

        def slow_delete(*args, **kwargs):
            time.sleep(0.1)  # hold the running window
            return {"success": True, "message": "pod deleted",
                    "namespace": "p", "pod_name": "x", "operation": "delete_pod"}

        with patch.object(wrappers, "delete_pod", side_effect=slow_delete):
            results: list[dict] = []
            barrier = threading.Barrier(2)

            def run():
                barrier.wait()  # synchronize start
                results.append(execute_step(plan.plan_id, 0, "fake-token"))

            t1 = threading.Thread(target=run)
            t2 = threading.Thread(target=run)
            t1.start(); t2.start()
            t1.join(); t2.join()

        # Exactly one success (this is the contract the CAS enforces).
        successes = [r for r in results if r.get("success") is True]
        failures = [r for r in results if r.get("success") is False]
        assert len(successes) == 1, f"expected exactly one success, got {results}"
        assert len(failures) == 1
        # The loser must see in_progress (or already_executed if it lost
        # the race AFTER the winner finished).
        loser_reason = failures[0].get("reason")
        assert loser_reason in ("in_progress", "already_executed"), (
            f"loser had unexpected reason: {loser_reason}"
        )

    def test_execute_on_done_returns_already_executed(self, strict_mode):
        """Acceptance #6."""
        from services.plans import build_plan, execute_step, plan_store

        plan = build_plan(
            issue="r",
            steps_input=[
                {"tool": "delete_pod", "args": {"namespace": "p", "pod_name": "x"}}
            ],
        )

        # Pre-mark the step as done.
        plan_store.complete_step(plan.plan_id, 0,
                                  result={"success": True, "message": "done"})

        # Now any further execute_step should bail.
        result = execute_step(plan.plan_id, 0, "fake-token")
        assert result["success"] is False
        assert result["reason"] == "already_executed"
        assert "step" in result  # prior state surfaced


# ── Token contract (acceptance #7) ──────────────────────────────────────────

class TestExecuteStepRequiresToken:
    """Each step still needs its own dry-run + confirmation_token from §2."""

    def test_missing_token_rejected_before_wrapper_called(self, strict_mode):
        from services.plans import build_plan, execute_step

        plan = build_plan(
            issue="r",
            steps_input=[
                {"tool": "delete_pod", "args": {"namespace": "p", "pod_name": "x"}}
            ],
        )

        # Empty token → reject WITHOUT touching the wrapper.
        from k8s import wrappers
        with patch.object(wrappers, "delete_pod") as mock_delete:
            result = execute_step(plan.plan_id, 0, "")
        assert result["success"] is False
        assert result["reason"] == "missing_token"
        mock_delete.assert_not_called()

    def test_wrapper_token_validation_propagates(self, strict_mode):
        """The wrapper's token rejection should propagate as step failure."""
        from services.plans import build_plan, execute_step, plan_store

        plan = build_plan(
            issue="r",
            steps_input=[
                {"tool": "delete_pod", "args": {"namespace": "p", "pod_name": "x"}}
            ],
        )

        # Mock wrapper to mimic the §2 token-rejection response shape.
        from k8s import wrappers
        bad_token_response = {
            "success": False,
            "operation": "delete_pod",
            "token_error": "invalid_token",
            "error": "confirmation_token required",
        }
        with patch.object(wrappers, "delete_pod", return_value=bad_token_response):
            result = execute_step(plan.plan_id, 0, "definitely-wrong-token")

        assert result["success"] is False
        # Step status now `failed` so caller can retry with a fresh token.
        retrieved = plan_store.get(plan.plan_id)
        assert retrieved.steps[0].status == "failed"

    def test_failed_step_can_be_retried(self, strict_mode):
        """After a failure, the step is `failed` not `done` — retry allowed."""
        from services.plans import build_plan, execute_step, plan_store

        plan = build_plan(
            issue="r",
            steps_input=[
                {"tool": "delete_pod", "args": {"namespace": "p", "pod_name": "x"}}
            ],
        )

        from k8s import wrappers
        calls = []

        def flaky(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {"success": False, "error": "transient", "operation": "delete_pod"}
            return {"success": True, "message": "ok", "operation": "delete_pod"}

        with patch.object(wrappers, "delete_pod", side_effect=flaky):
            first = execute_step(plan.plan_id, 0, "t1")
            assert first["success"] is False
            # Retry — different token (mimics a fresh dry-run).
            second = execute_step(plan.plan_id, 0, "t2")
            assert second["success"] is True

        retrieved = plan_store.get(plan.plan_id)
        assert retrieved.steps[0].status == "done"


# ── Error paths (sanity) ────────────────────────────────────────────────────

class TestExecuteStepErrorPaths:

    def test_plan_not_found(self, strict_mode):
        from services.plans import execute_step
        result = execute_step("plan_doesnotexist", 0, "tok")
        assert result["success"] is False
        assert result["reason"] == "plan_not_found"

    def test_invalid_step_index(self, strict_mode):
        from services.plans import build_plan, execute_step
        plan = build_plan(
            issue="r",
            steps_input=[
                {"tool": "delete_pod", "args": {"namespace": "p", "pod_name": "x"}}
            ],
        )
        result = execute_step(plan.plan_id, 99, "tok")
        assert result["success"] is False
        assert result["reason"] == "invalid_step"
