"""Playbook YAML schema — dataclasses that define the playbook structure.

A playbook is a declarative investigation template:

    name: crashloop
    description: Investigate CrashLoopBackOff pods
    triggers:
      - type: status
        pattern: CrashLoopBackOff
    params:
      - name: namespace
        required: false
        default: ""
      - name: pod_name
        required: true
    steps:
      - tool: describe_pod
        params: { namespace: "{{ namespace }}", pod_name: "{{ pod_name }}" }
        store_as: describe
      - tool: get_pod_logs
        params: { namespace: "{{ namespace }}", pod_name: "{{ pod_name }}", previous: true }
        store_as: logs_previous
        condition: "{{ status == 'CrashLoopBackOff' }}"
    synthesis:
      prompt: |
        Analyze this CrashLoopBackOff investigation:
        Pod: {{ pod_name }} in {{ namespace }}
        Describe: {{ describe }}
        Logs: {{ logs_previous }}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Optional


@dataclass(frozen=True)
class PlaybookTrigger:
    """Condition that activates a playbook.

    type: what to match against
      - "status": pod status string (CrashLoopBackOff, Pending, etc.)
      - "event": Kubernetes event reason
      - "error": regex against raw error text
      - "category": error_parser category (pod_crashloop, pod_oom, etc.)

    pattern: regex string to match against the trigger value.
             Case-insensitive matching is used.
    """
    type: Literal["status", "event", "error", "category"]
    pattern: str

    def matches(self, value: str) -> bool:
        """Test if a value matches this trigger."""
        if not value:
            return False
        return bool(re.search(self.pattern, value, re.IGNORECASE))


@dataclass(frozen=True)
class PlaybookParam:
    """A parameter that the playbook expects at runtime."""
    name: str
    required: bool = False
    default: Any = ""
    description: str = ""


@dataclass(frozen=True)
class PlaybookStep:
    """One tool invocation in the playbook sequence.

    tool: name of a tool in the tool_registry (e.g. "describe_pod", "get_pod_logs")
    params: dict of param_name -> value. Values can use {{ var }} templates
            referencing playbook params or previous step store_as keys.
    store_as: key under which this step's result is stored for later steps
              and the synthesis prompt. If omitted, defaults to the tool name.
    condition: optional Jinja-like expression. If it evaluates to falsy,
              the step is skipped. Supports simple {{ var == 'value' }} checks.
    on_error: what to do if the tool call fails
      - "continue" (default): log warning, store error, proceed to next step
      - "abort": stop the playbook, return partial results
    """
    tool: str
    params: dict[str, Any] = field(default_factory=dict)
    store_as: str = ""
    condition: str = ""
    on_error: Literal["continue", "abort"] = "continue"

    def __post_init__(self):
        # Default store_as to the tool name
        if not self.store_as:
            object.__setattr__(self, "store_as", self.tool)


@dataclass(frozen=True)
class Playbook:
    """A complete playbook definition.

    Loaded from YAML or constructed programmatically.
    """
    name: str
    description: str
    triggers: list[PlaybookTrigger] = field(default_factory=list)
    params: list[PlaybookParam] = field(default_factory=list)
    steps: list[PlaybookStep] = field(default_factory=list)
    synthesis_prompt: str = ""
    category: str = "general"
    severity_hint: str = ""  # "critical", "high", "medium", "low"
    tags: list[str] = field(default_factory=list)

    def matches(self, **kwargs) -> bool:
        """Test if any trigger matches the given keyword arguments.

        Usage:
            playbook.matches(status="CrashLoopBackOff")
            playbook.matches(category="pod_oom")
            playbook.matches(error="OOMKilled container xyz")
        """
        if not self.triggers:
            return False
        for trigger in self.triggers:
            value = kwargs.get(trigger.type, "")
            if trigger.matches(str(value)):
                return True
        return False
