"""Pilot registry — the thin framework behind KubeAstra Pilots.

A **Pilot is data, not code**: a name, the read-tool subset it may use
(``tool_scope``, for the optional prose/narration path), the narration prompt id,
any write actions it may propose (⊆ ``PROPOSABLE_ACTIONS``), and the settings flag
that gates it. Investigation/planning lives in the pure core
(``mcp/services/upgrade``) plus the ReAct agent; this module only declares which
Pilots exist and whether they are enabled.

See ``internal_docs/features/PILOTS_PLAN.md`` §Phase 0. Adding a backlog Pilot
later is a ``register(Pilot(...))`` call, not a re-architecture.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Pilot:
    name: str
    tool_scope: frozenset[str]
    narration_prompt: str
    actions: frozenset[str] = field(default_factory=frozenset)
    flag: str = ""
    description: str = ""

    @property
    def write_capable(self) -> bool:
        return bool(self.actions)


REGISTRY: dict[str, Pilot] = {}


def register(p: Pilot) -> Pilot:
    REGISTRY[p.name] = p
    return p


def get_pilot(name: str) -> Pilot | None:
    return REGISTRY.get(name)


def _flag_enabled(settings: Any, flag: str) -> bool:
    return bool(flag) and bool(getattr(settings, flag, False))


def enabled_pilots(settings: Any) -> list[Pilot]:
    """Pilots whose own flag is on.

    The master ``pilots_enabled`` gate is checked by the router, not here, so this
    stays a pure function of the per-pilot flags (easy to unit-test with a stub
    settings object).
    """
    return [p for p in REGISTRY.values() if _flag_enabled(settings, p.flag)]


def scoped_tool_names(pilot: Pilot, valid: "list[str] | set[str]") -> list[str]:
    """The pilot's ``tool_scope`` restricted to tools that actually exist on the
    ReAct surface. A name in the scope that isn't valid is dropped here and should
    be caught by a test (``test_pilots_framework``) rather than shipped.
    """
    valid_set = set(valid)
    return sorted(n for n in pilot.tool_scope if n in valid_set)


# ── Registered Pilots (v1 = Upgrade + GitOps Reconciliation) ──────────────────
# Phase 0 stubs. ``tool_scope`` uses EXISTING ReAct read tools as placeholders so
# it is a valid subset today; Phase 1 adds the upgrade-specific scan tools
# (scan_cluster_api_versions, list_installed_operators, …) and widens these.

register(
    Pilot(
        name="upgrade",
        flag="upgrade_pilot_enabled",
        narration_prompt="upgrade_readiness",
        tool_scope=frozenset(
            {
                "get_namespaces",
                "get_nodes",
                "describe_node",
                "list_helm_releases",
                "get_helm_release",
                "list_namespace_resources",
                "get_resource_graph",
            }
        ),
        description=(
            "Plan and safely apply a Kubernetes version / API-deprecation migration."
        ),
    )
)

register(
    Pilot(
        name="gitops_reconcile",
        flag="gitops_reconcile_enabled",
        narration_prompt="gitops_reconcile",
        tool_scope=frozenset(
            {
                "get_events",
                "describe_workload",
                "get_rollout_status",
                "get_pods",
                "investigate_workload",
                "get_resource_graph",
            }
        ),
        description=(
            "Explain why an Argo CD / Flux app is OutOfSync or Degraded and offer a fix."
        ),
    )
)
