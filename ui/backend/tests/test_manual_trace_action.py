"""Tests for the read-only "Trace source/config" manual follow-up action.

These cover the backend half of the User-Triggered Deeper Source/Config Trace:
the manual action is emitted in top-level suggested_actions only for a verified
deterministic root cause that points at source-managed config, carries a
follow_up_prompt (never an executable command), and bypasses recovery-command
review entirely.
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import react  # noqa: E402
from react import (  # noqa: E402
    _build_manual_trace_action,
    _detect_source_indicators,
    _extract_actions_from_steps,
)


def _verified_summary(root_cause: str, suggested_fix: str = "", **over) -> dict:
    summary = {
        "schema_version": "root_cause_summary.v1",
        "resource_name": over.get("resource_name", "jenkins-legacy-0"),
        "namespace": over.get("namespace", "ci"),
        "target": {"container": over.get("container", "jenkins")},
        "root_cause": root_cause,
        "suggested_fix": suggested_fix,
        "confidence": over.get("confidence", 0.95),
        "source_evidence": over.get(
            "source_evidence", "verified_deterministic_investigation"
        ),
    }
    return {"root_cause_summary": summary}


JENKINS_RC = (
    "Jenkins plugin pipeline-model-api 2.2114 is incompatible with the pinned "
    "Helm chart values; the plugin version in values.yaml must be bumped."
)


def test_jenkins_plugin_mismatch_emits_single_manual_action():
    result = _verified_summary(JENKINS_RC)
    actions = _extract_actions_from_steps(
        [], result, answer_text="", reviewer_provider=None, question="why is jenkins crashing?"
    )
    manual = [a for a in actions if a.get("action_kind") == "manual"]
    assert len(manual) == 1
    action = manual[0]
    assert action["label"] == "Trace source/config"


def test_manual_action_shape_is_non_executable():
    action = _build_manual_trace_action(_verified_summary(JENKINS_RC), "")
    assert action is not None
    assert action["action_kind"] == "manual"
    assert action.get("follow_up_prompt")
    assert "command" not in action
    assert "confirm" not in action
    # Carries structured context for the resubmitted prompt.
    assert "jenkins-legacy-0" in action["follow_up_prompt"]
    assert "ci" in action["follow_up_prompt"]
    assert "2.2114" in action["follow_up_prompt"]  # version name preserved


def test_follow_up_prompt_prefers_live_configmap_tools():
    action = _build_manual_trace_action(_verified_summary(JENKINS_RC), "")
    assert action is not None
    prompt = action["follow_up_prompt"]
    # Steers to live ConfigMap inspection (search by value, then read) + RAG.
    assert "search_configmaps" in prompt
    assert "get_configmap" in prompt
    assert "kb_search" in prompt
    # The known failing version is offered as the search query.
    assert "search for 2.2114" in prompt
    # Repo tools the ReAct agent cannot call must NOT be advertised.
    assert "search_deployment_repo" not in prompt
    assert "get_deployment_repo_file" not in prompt
    # Secret values stay out of scope.
    assert "Never read Secret values" in prompt


def test_manual_action_bypasses_recovery_review():
    # Executable actions fail closed without a reviewer_provider. The manual
    # action must still appear, proving it never went through _review_recovery_action.
    result = _verified_summary(JENKINS_RC)
    actions = _extract_actions_from_steps(
        [], result, answer_text="", reviewer_provider=None, question=""
    )
    assert any(a.get("action_kind") == "manual" for a in actions)


def test_unverified_root_cause_does_not_emit():
    result = _verified_summary(
        JENKINS_RC, confidence=0.75, source_evidence="deterministic_investigation"
    )
    assert _build_manual_trace_action(result, "") is None


def test_confidence_threshold_alone_is_sufficient():
    result = _verified_summary(
        JENKINS_RC, confidence=0.95, source_evidence="deterministic_investigation"
    )
    assert _build_manual_trace_action(result, "") is not None


def test_no_root_cause_summary_returns_none():
    assert _build_manual_trace_action({}, "") is None
    assert _build_manual_trace_action({"root_cause_summary": None}, "") is None


def test_verified_but_no_source_indicator_does_not_emit():
    # Verified root cause with no source/config angle (pure runtime OOMKill).
    result = _verified_summary("Container was OOMKilled after exceeding its memory limit.")
    assert _build_manual_trace_action(result, "") is None


def test_generic_configmap_mention_without_context_does_not_emit():
    result = _verified_summary("The pod reads a ConfigMap and a Secret at startup.")
    assert _build_manual_trace_action(result, "") is None


def test_configmap_with_mount_context_emits():
    result = _verified_summary(
        "The mounted ConfigMap volume defines the wrong log level for the app."
    )
    assert _build_manual_trace_action(result, "") is not None


def test_total_actions_capped_at_five_with_manual(monkeypatch):
    # When many executable actions exist, appending the manual trace must not
    # push the total past 5: cap is 4 executable + 1 manual.
    cmds = [f"kubectl rollout restart deployment/app{i} -n ci" for i in range(6)]
    monkeypatch.setattr(react, "_extract_kubectl_commands", lambda text: cmds)
    monkeypatch.setattr(
        react,
        "_review_recovery_action",
        lambda action, **kw: {**action, "type": "apply", "confirm": True},
    )

    actions = _extract_actions_from_steps(
        [],
        _verified_summary(JENKINS_RC),
        answer_text="answer with executable fixes",
        reviewer_provider=object(),
        question="",
    )

    assert len(actions) == 5
    manual = [a for a in actions if a.get("action_kind") == "manual"]
    assert len(manual) == 1
    assert actions[-1]["action_kind"] == "manual"


def test_total_actions_capped_at_five_without_manual(monkeypatch):
    cmds = [f"kubectl rollout restart deployment/app{i} -n ci" for i in range(6)]
    monkeypatch.setattr(react, "_extract_kubectl_commands", lambda text: cmds)
    monkeypatch.setattr(
        react,
        "_review_recovery_action",
        lambda action, **kw: {**action, "type": "apply", "confirm": True},
    )

    # Verified summary but no source indicator -> no manual action -> 5 executable.
    actions = _extract_actions_from_steps(
        [],
        _verified_summary("Container was OOMKilled after exceeding its memory limit."),
        answer_text="answer with executable fixes",
        reviewer_provider=object(),
        question="",
    )

    assert len(actions) == 5
    assert all(a.get("action_kind") != "manual" for a in actions)


def test_detect_source_indicators_is_narrow():
    assert "helm" in _detect_source_indicators("helm values define this")
    # Generic ConfigMap mention with no mount/reference/pin context is ignored.
    assert _detect_source_indicators("just a configmap") == []
    assert "configmap" in _detect_source_indicators("the mounted configmap reference")
