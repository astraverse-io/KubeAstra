"""Tests for the playbook engine — schema, loader, registry, runner.

These tests validate:
- YAML schema parsing and dataclass construction
- Trigger matching logic
- Template substitution in step params
- Condition evaluation
- Built-in playbooks load correctly
- Registry lookup and matching
"""

import pytest
import sys
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure mcp/ is on the path
MCP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)


from playbooks.schema import Playbook, PlaybookStep, PlaybookTrigger, PlaybookParam
from playbooks.loader import load_all, load_file, BUILTINS_DIR
from playbooks.registry import (
    get_playbook,
    list_playbooks,
    match_playbooks,
    register_playbook,
    reload,
)
from playbooks.runner import (
    _template_string,
    _template_params,
    _evaluate_condition,
    PlaybookResult,
    StepResult,
)


# ── Schema tests ────────────────────────────────────────────────────────────

class TestPlaybookTrigger:
    def test_status_trigger_matches(self):
        t = PlaybookTrigger(type="status", pattern="CrashLoopBackOff")
        assert t.matches("CrashLoopBackOff")
        assert not t.matches("Running")

    def test_status_trigger_case_insensitive(self):
        t = PlaybookTrigger(type="status", pattern="CrashLoopBackOff")
        assert t.matches("crashloopbackoff")

    def test_regex_trigger(self):
        t = PlaybookTrigger(type="error", pattern="OOMKilled|OutOfMemory")
        assert t.matches("Container was OOMKilled")
        assert t.matches("OutOfMemory exception")
        assert not t.matches("Container running fine")

    def test_empty_value_no_match(self):
        t = PlaybookTrigger(type="status", pattern="CrashLoopBackOff")
        assert not t.matches("")
        assert not t.matches(None)


class TestPlaybook:
    def test_matches_with_status(self):
        pb = Playbook(
            name="test",
            description="test",
            triggers=[PlaybookTrigger(type="status", pattern="CrashLoopBackOff")],
        )
        assert pb.matches(status="CrashLoopBackOff")
        assert not pb.matches(status="Running")

    def test_matches_multiple_triggers(self):
        pb = Playbook(
            name="test",
            description="test",
            triggers=[
                PlaybookTrigger(type="status", pattern="CrashLoopBackOff"),
                PlaybookTrigger(type="category", pattern="pod_crashloop"),
            ],
        )
        assert pb.matches(status="CrashLoopBackOff")
        assert pb.matches(category="pod_crashloop")
        assert not pb.matches(status="Running")

    def test_no_triggers_no_match(self):
        pb = Playbook(name="test", description="test")
        assert not pb.matches(status="anything")


class TestPlaybookStep:
    def test_store_as_defaults_to_tool(self):
        step = PlaybookStep(tool="describe_pod")
        assert step.store_as == "describe_pod"

    def test_store_as_override(self):
        step = PlaybookStep(tool="get_pod_logs", store_as="logs_previous")
        assert step.store_as == "logs_previous"


# ── Template tests ──────────────────────────────────────────────────────────

class TestTemplating:
    def test_simple_substitution(self):
        result = _template_string("{{ namespace }}", {"namespace": "prod"})
        assert result == "prod"

    def test_mixed_content(self):
        result = _template_string(
            "Pod {{ pod_name }} in {{ namespace }}",
            {"pod_name": "my-app", "namespace": "prod"},
        )
        assert result == "Pod my-app in prod"

    def test_missing_var_kept(self):
        result = _template_string("{{ unknown }}", {})
        assert result == "{{ unknown }}"

    def test_non_string_passthrough(self):
        """A single {{ var }} referencing a non-string returns the raw value."""
        result = _template_string("{{ previous }}", {"previous": True})
        assert result is True

    def test_dict_passthrough(self):
        ctx = {"data": {"key": "value"}}
        result = _template_string("{{ data }}", ctx)
        assert result == {"key": "value"}

    def test_template_params_recursive(self):
        params = {
            "namespace": "{{ ns }}",
            "nested": {"pod": "{{ pod_name }}"},
        }
        result = _template_params(params, {"ns": "demo", "pod_name": "my-app"})
        assert result == {"namespace": "demo", "nested": {"pod": "my-app"}}


# ── Condition tests ─────────────────────────────────────────────────────────

class TestConditions:
    def test_truthy_check(self):
        assert _evaluate_condition("{{ pod_name }}", {"pod_name": "my-app"})
        assert not _evaluate_condition("{{ pod_name }}", {"pod_name": ""})

    def test_equality_check(self):
        ctx = {"status": "CrashLoopBackOff"}
        assert _evaluate_condition("{{ status == 'CrashLoopBackOff' }}", ctx)
        assert not _evaluate_condition("{{ status == 'Running' }}", ctx)

    def test_inequality_check(self):
        ctx = {"status": "Running"}
        assert _evaluate_condition("{{ status != 'CrashLoopBackOff' }}", ctx)
        assert not _evaluate_condition("{{ status != 'Running' }}", ctx)

    def test_missing_var_is_falsy(self):
        assert not _evaluate_condition("{{ nonexistent }}", {})


# ── Loader tests ────────────────────────────────────────────────────────────

class TestLoader:
    def test_builtins_exist(self):
        assert BUILTINS_DIR.is_dir()
        yamls = list(BUILTINS_DIR.glob("*.yaml")) + list(BUILTINS_DIR.glob("*.yml"))
        assert len(yamls) >= 7, f"Expected at least 7 built-in playbooks, found {len(yamls)}"

    def test_load_all_returns_playbooks(self):
        playbooks = load_all()
        assert len(playbooks) >= 7
        names = {pb.name for pb in playbooks}
        assert "crashloop" in names
        assert "oomkilled" in names
        assert "imagepull" in names
        assert "pending_pod" in names
        assert "pvc_stuck" in names
        assert "node_pressure" in names
        assert "deployment_stuck" in names

    def test_each_builtin_has_required_fields(self):
        for pb in load_all():
            assert pb.name, f"Playbook missing name"
            assert pb.description, f"Playbook '{pb.name}' missing description"
            assert pb.triggers, f"Playbook '{pb.name}' has no triggers"
            assert pb.steps, f"Playbook '{pb.name}' has no steps"
            assert pb.synthesis_prompt, f"Playbook '{pb.name}' has no synthesis prompt"

    def test_crashloop_playbook_structure(self):
        playbooks = load_all()
        crashloop = next(pb for pb in playbooks if pb.name == "crashloop")
        assert crashloop.category == "pod"
        assert crashloop.severity_hint == "critical"
        assert len(crashloop.steps) == 4
        assert crashloop.steps[0].tool == "describe_pod"
        assert crashloop.steps[3].tool == "get_events"
        # Has namespace and pod_name params
        param_names = {p.name for p in crashloop.params}
        assert "namespace" in param_names
        assert "pod_name" in param_names


# ── Registry tests ──────────────────────────────────────────────────────────

class TestRegistry:
    @classmethod
    def setup_class(cls):
        reload()

    def test_get_playbook_by_name(self):
        pb = get_playbook("crashloop")
        assert pb is not None
        assert pb.name == "crashloop"

    def test_get_nonexistent_returns_none(self):
        assert get_playbook("nonexistent") is None

    def test_list_playbooks(self):
        pbs = list_playbooks()
        assert len(pbs) >= 7
        # Should be sorted by name
        names = [pb.name for pb in pbs]
        assert names == sorted(names)

    def test_match_by_status(self):
        matches = match_playbooks(status="CrashLoopBackOff")
        names = {pb.name for pb in matches}
        assert "crashloop" in names

    def test_match_by_category(self):
        matches = match_playbooks(category="pod_oom")
        names = {pb.name for pb in matches}
        assert "oomkilled" in names

    def test_match_imagepull(self):
        matches = match_playbooks(status="ImagePullBackOff")
        names = {pb.name for pb in matches}
        assert "imagepull" in names

    def test_match_pending(self):
        matches = match_playbooks(status="Pending")
        names = {pb.name for pb in matches}
        assert "pending_pod" in names

    def test_no_match(self):
        matches = match_playbooks(status="Running")
        assert len(matches) == 0

    def test_register_custom_playbook(self):
        custom = Playbook(
            name="test_custom",
            description="A test playbook",
            triggers=[PlaybookTrigger(type="status", pattern="TestStatus")],
            steps=[PlaybookStep(tool="get_pods", params={"namespace": "test"})],
        )
        register_playbook(custom)
        assert get_playbook("test_custom") is not None
        matches = match_playbooks(status="TestStatus")
        assert any(pb.name == "test_custom" for pb in matches)
