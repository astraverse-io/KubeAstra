from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class SupportedAlertPattern(BaseModel):
    pattern: str
    fields: list[str] = Field(default_factory=lambda: ["name", "annotations.summary"])


class SafetyPolicy(BaseModel):
    readonly: bool = True
    max_steps: int = Field(default=6, ge=1, le=30)
    blocked_verbs: list[str] = Field(
        default_factory=lambda: ["create", "update", "patch", "delete", "scale", "restart", "exec"]
    )


class LlmPolicy(BaseModel):
    mode: str = "advisory_only"
    require_structured_output: bool = True
    may_select_tools_only_from_allowed_tools: bool = True


class ExecutionPolicy(BaseModel):
    mode: str = "bounded_llm"
    allow_only_playbook_steps: bool = True
    allow_llm_suggested_steps: bool = True
    auto_execute_llm_suggestions: bool = False
    require_human_review_for_new_steps: bool = True


class PlaybookStep(BaseModel):
    id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    when: str | None = None
    description: str | None = None
    # When True the orchestrator dispatches this step BEFORE consulting the
    # LLM — guaranteeing composite "super-tools" like investigate_pod always
    # run first instead of being passed over for piecemeal describe_pod /
    # get_pod_logs / get_events calls that miss per-container context.
    deterministic: bool = False


class Playbook(BaseModel):
    id: str
    version: str
    display_name: str
    supported_alerts: list[SupportedAlertPattern]
    allowed_tools: list[str]
    safety: SafetyPolicy = Field(default_factory=SafetyPolicy)
    llm_policy: LlmPolicy = Field(default_factory=LlmPolicy)
    execution_policy: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    steps: list[PlaybookStep]
    decision_rules: list[dict[str, Any]] = Field(default_factory=list)
    rca_scoring: dict[str, Any] = Field(default_factory=dict)
    remediation_mappings: dict[str, list[str]] = Field(default_factory=dict)
    stop_conditions: dict[str, Any] = Field(default_factory=dict)
    validation_warnings: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_step_tools(self) -> Playbook:
        unknown = [step.tool for step in self.steps if step.tool not in self.allowed_tools]
        if unknown:
            raise ValueError(f"Playbook steps reference tools outside allowed_tools: {unknown}")
        if not self.safety.readonly:
            raise ValueError("Playbooks must be read-only")
        if self.execution_policy.auto_execute_llm_suggestions:
            raise ValueError("LLM candidate steps must not be auto-executed")
        return self
