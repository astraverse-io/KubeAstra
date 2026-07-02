"""Deterministic tool scoping (harness Phase 7).

Maps a user prompt to a *scope* — a small whitelist of tools the ReAct loop is
allowed to call for this turn. The narrower toolbox reduces the number of wrong
choices the LLM can make. Today's baseline (post-Sprint-3 of the synthesis
plan) shows ~47% tool-choice accuracy (7/15 calibration scenarios); the Phase 7
target is ≥80% (12/15).

Design (matches AGENT_HARNESS_IMPLEMENTATION_PLAN.md Phase 7):

- **Classifier**: deterministic keyword/regex heuristics. Cheap, no extra LLM
  call. Anything that doesn't match falls back to ``broad`` (no scoping
  applied), so we never over-restrict on a prompt we don't recognize.
- **Escape hatches**: ``find_workload``, ``get_namespaces``, ``kb_search`` are
  always included in every scope so the agent can discover/recover when its
  initial assumption is wrong.
- **Recovery**: an out-of-scope tool call is intercepted by ``react.py`` and
  surfaced to the agent as ``tool_out_of_scope`` with a hint listing the
  current allowed tools. The agent gets to retry with a different choice.

This module is **provider-agnostic** and has no LLM or DB dependencies — it's
pure functions over the prompt string, safe to unit-test in isolation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Universally-available recovery tools. Every scope inherits these so the
# agent can ask the cluster "what exists?" or "what's in our docs?" without
# an explicit "expand scope" step. ``find_workload`` is deliberately NOT
# universal — for rollout/service-shaped prompts the entity is always named
# directly in the question, so encouraging a discovery hop just steers the
# agent away from the targeted tool.
ESCAPE_HATCHES: frozenset[str] = frozenset({
    "get_namespaces",
    "kb_search",
})

# Per-scope tool whitelists. Each set will be unioned with ESCAPE_HATCHES at
# scope-resolution time. ``find_workload`` is included only where it's
# genuinely useful (pod debugging, inventory, knowledge); excluded from
# rollout and service because those prompts always name the entity.
# ``analyze_error`` is intentionally excluded from pod_debug — the eval
# baseline showed the agent picking the AI synthesis tool instead of
# ``investigate_pod``, so we remove the temptation.
SCOPE_TOOLS: dict[str, frozenset[str]] = {
    "pod_debug": frozenset({
        "investigate_pod", "get_pod_logs", "get_events", "get_pods",
        "describe_pod", "get_deployment", "get_rollout_status",
        "find_workload",
    }),
    "rollout": frozenset({
        "get_deployment", "get_rollout_status", "get_pods", "get_events",
        "get_resource_graph", "investigate_workload", "find_workload",
    }),
    "service": frozenset({
        "get_service", "get_endpoints", "list_services", "get_pods",
        "get_resource_graph", "find_workload",
    }),
    "inventory": frozenset({
        "get_namespaces", "get_nodes", "get_pods", "list_services",
        "list_namespace_resources", "list_kubeconfig_contexts",
        "find_workload",
    }),
    "knowledge": frozenset({
        "kb_search", "generate_runbook", "get_fix_commands",
    }),
}


# Heuristics, evaluated top-to-bottom — more specific scopes first. The first
# pattern that matches wins. Patterns are compiled case-insensitively over the
# whole prompt. We deliberately favor recall over precision: a wrong "broad"
# fallback just gives the agent the full toolbox (today's behavior), but a
# wrong narrow scope blocks the right tool. Better to err toward catching
# debug-shaped prompts.
_SCOPE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Knowledge / runbook — placed first because these often mention pods or
    # services as nouns but are really about documentation, not live state.
    ("knowledge", re.compile(
        r"\b("
        r"runbook|playbook"
        r"|how\s+(?:do|should|can|would)\s+(?:we|i|you)\s+(?:recover|handle|respond|deal|fix|address)"
        r"|according\s+to\s+(?:our|the)\s+(?:docs|runbook|playbook|guide|team)"
        r"|what\s+(?:does|is)\s+(?:our|the)\s+(?:team|policy|runbook|process)"
        r"|standard\s+procedure|escalation\s+path"
        r"|ansible\s+(?:playbook|task|role)"
        r")\b",
        re.IGNORECASE,
    )),
    # Pod debugging — broad: any pod-shaped failure or investigation.
    ("pod_debug", re.compile(
        r"("
        # Specific failure signals — unambiguous.
        r"\b(crash(?:ing|ed|loop)?(?:back\s*off)?)\b"
        r"|\bimagepullbackoff\b|\berrimagepull\b"
        r"|\boom(?:killed|kill)?\b"
        r"|\bprobe(?:s)?\b|\bliveness\b|\breadiness\b|\bstartupprobe\b"
        # "investigate ... pod" within ~60 chars (handles "investigate the api-pod").
        r"|\binvestigate\b[^.?!]{0,60}?\bpod\b"
        r"|\binvestigate\b[^.?!]{0,40}-pod\b"
        # "debug the pod" / "pod debug".
        r"|\bdebug\b[^.?!]{0,30}\bpod\b|\bpod\b[^.?!]{0,15}\bdebug\b"
        # "why is the pod ..." / "why did my pod ...".
        r"|\bwhy\s+(?:is|did|are)\s+(?:my\s+|the\s+|a\s+|an\s+)?[^.?!]{0,40}\bpod"
        # "pod X is crashing/failing/stuck/...".
        r"|\bpod\b[^.?!]{0,30}\b(?:crash(?:ing|ed)?|fail(?:ed|ing)?|stuck|pending|terminating|evicted|restart(?:ing)?|error|broken|down)\b"
        # "logs of/from/for X pod" / "pod logs" / "log severity/analysis/warnings/errors".
        r"|\blogs?\s+(?:of|from|for)\s+[^.?!]{0,40}\bpod\b"
        r"|\bpod\b[^.?!]{0,15}\blogs?\b"
        r"|\blog\s+(?:severity|analysis|warnings?|errors?)\b"
        # "X-pod is/was failing/crashing/..." (hyphenated names).
        r"|\b[a-z0-9-]+-pod\b"
        r")",
        re.IGNORECASE,
    )),
    # Rollout / deployment status.
    ("rollout", re.compile(
        r"\b("
        r"rollout(?:s)?"
        r"|deploy(?:ment)?\s*(?:status|stuck|fail|drift|progress|replica)"
        r"|replicas?\s+(?:ready|drift|mismatch|scaling|for|of|in)"
        r"|scaling\s+up|scaling\s+down"
        r"|rolling\s+(?:update|restart|out|back)"
        r"|stuck\s+rolling"
        r"|desired\s+vs\.?\s*ready"
        r")\b",
        re.IGNORECASE,
    )),
    # Service / endpoint connectivity.
    ("service", re.compile(
        r"("
        r"\bservice\b[^.?!]{0,60}\bendpoints?\b"
        r"|\bendpoints?\b[^.?!]{0,30}\b(?:missing|empty|none|wrong|down|not\s+ready|fail)\b"
        r"|\bno\s+endpoints?\b"
        r"|\bingress\s+(?:not\s+routing|misroute|down)\b"
        r"|\b(?:503|502|504)\b[^.?!]{0,60}\b(?:service|endpoint|ingress)\b"
        r"|\bservice\b[^.?!]{0,60}\b(?:503|502|504)\b"
        r"|\bservice\s+selector\s+matches?\s+no\s+pods?\b"
        r"|\bcheck\b[^.?!]{0,20}\bendpoints?\b"
        r")",
        re.IGNORECASE,
    )),
    # General cluster inventory / listing.
    ("inventory", re.compile(
        r"\b("
        r"list\s+(?:(?:all|the)\s+)?(?:namespaces?|nodes?|pods?|services?|deployments?)"
        r"|show\s+(?:me\s+)?(?:(?:all|the)\s+)?(?:namespaces?|nodes?|pods?|services?)"
        r"|what\s+(?:namespaces?|nodes?)\s+(?:exist|are\s+there|do\s+we\s+have)"
        r"|which\s+(?:namespaces?|nodes?|pods?|services?)"
        r"|how\s+many\s+(?:pods?|nodes?|services?)"
        r"|get\s+(?:(?:all|the)\s+)?(?:namespaces?|nodes?|pods?)"
        r")\b",
        re.IGNORECASE,
    )),
]


@dataclass(frozen=True)
class ScopeDecision:
    """Result of scoping a single user prompt."""
    scope_name: str                      # e.g., "pod_debug", "broad"
    allowed_tools: frozenset[str]        # whitelist (incl. escape hatches)
    matched_pattern: Optional[str]       # for logging / debugging; None if fallback

    def is_broad(self) -> bool:
        return self.scope_name == "broad"

    def to_dict(self) -> dict:
        """Serializable form for ``agent_runs.tool_scope_json``."""
        return {
            "scope": self.scope_name,
            "allowed_tools": sorted(self.allowed_tools),
            "matched_pattern": self.matched_pattern,
        }


def scope_for_prompt(
    prompt: str,
    *,
    available_tools: Optional[frozenset[str]] = None,
) -> ScopeDecision:
    """Classify a user prompt into a scope.

    If ``available_tools`` is provided, the resulting allowed set is intersected
    with it so we never advertise a tool the registry doesn't expose. When
    nothing matches, returns the ``broad`` scope (no restriction — caller
    should treat as "include every available react tool").
    """
    text = (prompt or "").strip()
    if not text:
        return ScopeDecision(
            scope_name="broad",
            allowed_tools=available_tools or frozenset(),
            matched_pattern=None,
        )

    for name, pattern in _SCOPE_PATTERNS:
        m = pattern.search(text)
        if m:
            tools = SCOPE_TOOLS[name] | ESCAPE_HATCHES
            if available_tools is not None:
                tools = frozenset(tools) & available_tools
            return ScopeDecision(
                scope_name=name,
                allowed_tools=tools,
                matched_pattern=m.group(0),
            )

    # Fallback: broad scope, full toolbox.
    return ScopeDecision(
        scope_name="broad",
        allowed_tools=available_tools or frozenset(),
        matched_pattern=None,
    )
