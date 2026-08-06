"""Unified tool registry — single source of truth for all KubeAstra tools.

Every tool across all surfaces (MCP stdio, HTTP MCP, REST chat, ReAct loop)
is defined here with its metadata, schema, and handler adapter. Entry points
import from this module instead of maintaining their own tool lists.

Phase 2 of the routing architecture refactoring.
See docs/ROUTING_ARCHITECTURE_PROPOSAL_FIXES.md for context.

Usage:
    from tool_registry import resolve_tool, tools_for_surface, dispatch, DispatchContext
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Literal, Optional

logger = logging.getLogger(__name__)

# ── Types ────────────────────────────────────────────────────────────────────

ToolSurface = Literal["mcp", "chat", "react", "rest", "playbook"]


# ── Core data structures ────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolDef:
    """Metadata and execution adapter for a single tool."""
    name: str                                            # Canonical name (e.g. "list_kubeconfig_contexts")
    handler: Callable[[dict, DispatchContext], dict]      # Adapter wrapping the raw implementation
    schema: type                                         # Pydantic BaseModel input class
    description: str                                     # Human-readable, used by all prompts
    category: str                                        # Grouping key: investigation, discovery, pod, etc.
    surfaces: frozenset[ToolSurface]                      # Where this tool is available
    aliases: tuple[str, ...] = ()                        # Short names that resolve to this tool
    write_op: bool = False                               # Is this a write/destructive operation?
    requires_confirm: bool = False                       # Does it need confirm=True to execute?
    react_enabled: bool = True                           # Should the ReAct agent see this tool?
    returns_json_string: bool = False                    # AI tools return str, not dict
    notes: str = ""                                      # Implementation notes (not user-facing)


@dataclass
class DispatchContext:
    """Execution context passed to every handler adapter."""
    surface: ToolSurface
    session_id: Optional[str] = None
    history: Optional[list] = None
    allow_write: bool = False


# ── Name resolution helpers ─────────────────────────────────────────────────
# Copied from chat.py _resolve_pod_ns_and_name / _candidate_workload_names.
# chat.py keeps its copies until Phase 5 wires up the shared dispatcher.

def _candidate_workload_names(raw_name: str) -> list[str]:
    """Generate likely Kubernetes resource names from a natural-language phrase."""
    if not raw_name:
        return []

    cleaned = re.sub(r"[^a-z0-9\s._-]", " ", raw_name.lower()).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return []

    tokens = [t for t in cleaned.split(" ") if t]
    generic_suffixes = {"pod", "deployment", "service", "app", "application", "workload"}

    candidates: list[str] = []

    def _push(name: str) -> None:
        name = re.sub(r"[-_.]{2,}", "-", name.strip("-._"))
        if name and name not in candidates:
            candidates.append(name)

    _push(cleaned.replace(" ", "-"))
    _push(cleaned.replace(" ", ""))

    if len(tokens) > 1 and tokens[-1] in generic_suffixes:
        trimmed = tokens[:-1]
        if trimmed:
            _push("-".join(trimmed))
            _push("".join(trimmed))

    return candidates


def _resolve_pod_ns_and_name(params: dict, pod_name: str) -> tuple[str, str]:
    """Return (namespace, exact_pod_name) for pod-specific tool calls.

    Runs find_workload to resolve partial/prefix pod names to the first
    matching running pod. When namespace is explicitly given, results are
    filtered to that namespace.
    """
    explicit_ns = params.get("namespace")
    if not pod_name:
        return explicit_ns or "default", pod_name

    candidates = _candidate_workload_names(pod_name) or [pod_name]

    if explicit_ns:
        try:
            from k8s.wrappers import get_pods
            namespace_pods = get_pods(explicit_ns).get("pods", [])
            for candidate in candidates:
                candidate_l = candidate.lower()
                exact = [
                    p for p in namespace_pods
                    if p.get("name", "").lower() == candidate_l
                ]
                prefixed = [
                    p for p in namespace_pods
                    if p.get("name", "").lower().startswith(candidate_l)
                ]
                contains = [
                    p for p in namespace_pods
                    if candidate_l in p.get("name", "").lower()
                ]
                matches = exact or prefixed or contains
                if matches:
                    return explicit_ns, matches[0].get("name", candidate)
        except Exception:
            pass

    for candidate in candidates:
        try:
            from k8s.wrappers import find_workload
            fw = find_workload(candidate)
            pods = fw.get("pods", [])
            deps = fw.get("deployments", [])

            if explicit_ns:
                ns_pods = [p for p in pods if p.get("namespace") == explicit_ns]
                ns_deps = [d for d in deps if d.get("namespace") == explicit_ns]
                pods = ns_pods or pods
                deps = ns_deps or deps

            if pods:
                first = pods[0]
                return first.get("namespace") or explicit_ns or "default", first.get("name", candidate)

            if deps:
                return deps[0].get("namespace") or explicit_ns or "default", candidate
        except Exception:
            pass

    return explicit_ns or "default", pod_name


# ── Handler adapters ────────────────────────────────────────────────────────
# Each handler wraps the raw implementation function with the exact behavior
# currently in _dispatch_inner (namespace resolution, auto-discovery, JSON
# parsing for AI tools). Signatures: (params: dict, ctx: DispatchContext) -> dict

# -- Investigation tools --

def _handle_investigate_pod(params: dict, ctx: DispatchContext) -> Any:
    import time
    from k8s.wrappers import investigate_pod

    start_time = time.perf_counter()
    pod_name = params.get("pod_name", "")
    if ctx.surface in ("chat", "react"):
        ns, pod_name = _resolve_pod_ns_and_name(params, pod_name)
    else:
        ns = params.get("namespace") or "default"

    raw_result = investigate_pod(
        ns, pod_name,
        tail=params.get("tail", 200),
        use_ai=params.get("use_ai", True),
    )

    # Only wrap in ToolEnvelope for the ReAct surface. MCP / chat single-shot
    # / REST / playbook surfaces keep the legacy raw shape so we don't
    # silently break external consumers (Cursor, HTTP MCP clients, etc.).
    if ctx.surface == "react":
        from services.tool_envelope import make_investigate_pod_envelope
        duration_ms = (time.perf_counter() - start_time) * 1000
        return make_investigate_pod_envelope(raw_result, params, duration_ms)
    return raw_result



def _handle_investigate_workload(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import investigate_workload
    ns = params.get("namespace") or "default"
    return investigate_workload(
        ns,
        params.get("workload_name", ""),
        params.get("workload_type", "deployment"),
        use_ai=params.get("use_ai", True),
    )


def _handle_analyze_namespace(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import analyze_namespace
    return analyze_namespace(params.get("namespace") or "default")


# -- Discovery tools --

def _handle_find_workload(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import find_workload
    return find_workload(params["name"], params.get("environment"))


def _handle_get_namespaces(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import get_namespaces
    return get_namespaces()


def _handle_get_nodes(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import get_nodes
    return get_nodes(
        params.get("node_name") or params.get("name"),
        labels_only=bool(params.get("labels_only", False)),
        taints_only=bool(params.get("taints_only", False)),
        conditions_only=bool(params.get("conditions_only", False)),
        addresses_only=bool(params.get("addresses_only", False)),
    )


def _handle_investigate_node(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import investigate_node
    return investigate_node(params.get("node_name") or params.get("name") or "")


def _handle_list_namespace_resources(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import list_namespace_resources
    return list_namespace_resources(params.get("namespace") or "default")


def _handle_get_configmap(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import get_configmap
    return get_configmap(params["namespace"], params["name"], params.get("key"))


def _handle_search_configmaps(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import search_configmaps
    return search_configmaps(params["namespace"], params["query"], params.get("max_matches"))


def _handle_helm_available(params: dict, ctx: DispatchContext) -> dict:
    from k8s.helm_wrappers import helm_available
    return helm_available()


def _handle_list_helm_releases(params: dict, ctx: DispatchContext) -> dict:
    from k8s.helm_wrappers import list_helm_releases
    return list_helm_releases(
        params.get("namespace"), bool(params.get("all_namespaces")), params.get("status_filter")
    )


def _handle_get_helm_release(params: dict, ctx: DispatchContext) -> dict:
    from k8s.helm_wrappers import get_helm_release
    return get_helm_release(
        params["release"], params["namespace"], params.get("sections"), params.get("revision")
    )


def _handle_diff_helm_revisions(params: dict, ctx: DispatchContext) -> dict:
    from k8s.helm_wrappers import diff_helm_revisions
    return diff_helm_revisions(
        params["release"], params["namespace"],
        params["from_revision"], params["to_revision"],
        params.get("section", "values"),
    )


def _handle_investigate_helm_release(params: dict, ctx: DispatchContext) -> dict:
    from k8s.helm_wrappers import investigate_helm_release
    return investigate_helm_release(params["release"], params["namespace"])


# -- Pod tools --

def _handle_get_pods(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import get_pods
    ns = params.get("namespace") or "default"
    return get_pods(
        ns,
        params.get("label_selector"),
        params.get("status_filter"),
        params.get("exclude_namespaces"),
        params.get("exclude_namespace_prefixes"),
        labels_only=bool(params.get("labels_only", False)),
        images_only=bool(params.get("images_only", False)),
        resources_only=bool(params.get("resources_only", False)),
        placement_only=bool(params.get("placement_only", False)),
        details=bool(params.get("details", False)),
    )


def _handle_get_pod_logs(params: dict, ctx: DispatchContext) -> Any:
    import time
    from k8s.wrappers import get_pod_logs

    start_time = time.perf_counter()
    pod_name = params.get("pod_name", "")
    if ctx.surface in ("chat", "react"):
        ns, pod_name = _resolve_pod_ns_and_name(params, pod_name)
    else:
        ns = params.get("namespace") or "default"

    raw_result = get_pod_logs(
        ns, pod_name,
        previous=params.get("previous", False),
        tail=params.get("tail", 200),
        container=params.get("container"),
    )

    # See _handle_investigate_pod: envelopes only for the ReAct surface.
    if ctx.surface == "react":
        from services.tool_envelope import make_pod_logs_envelope
        duration_ms = (time.perf_counter() - start_time) * 1000
        return make_pod_logs_envelope(raw_result, params, duration_ms)
    return raw_result



def _handle_describe_pod(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import describe_pod
    return describe_pod(params.get("namespace") or "default", params.get("pod_name", ""))


# -- Cluster state tools --

def _handle_get_events(params: dict, ctx: DispatchContext) -> Any:
    import time
    from k8s.wrappers import get_events

    start_time = time.perf_counter()
    ns = params.get("namespace") or "default"
    field_selector = params.get("field_selector")
    raw_result = get_events(ns, field_selector)

    # See _handle_investigate_pod: envelopes only for the ReAct surface.
    if ctx.surface == "react":
        from services.tool_envelope import make_events_envelope
        duration_ms = (time.perf_counter() - start_time) * 1000
        return make_events_envelope(raw_result, params, duration_ms)
    return raw_result



def _handle_get_recent_changes(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import get_recent_changes

    return get_recent_changes(
        params.get("namespace") or "default",
        within_minutes=int(params.get("within_minutes") or 60),
        workload_name=params.get("workload_name"),
    )


def _handle_get_deployment(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import get_deployment
    return get_deployment(
        params.get("namespace") or "default",
        params.get("deployment_name", ""),
        labels_only=bool(params.get("labels_only", False)),
        images_only=bool(params.get("images_only", False)),
        resources_only=bool(params.get("resources_only", False)),
        template_only=bool(params.get("template_only", False)),
    )


def _handle_get_service(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import get_service
    ns = params.get("namespace") or "default"
    svc_name = params.get("service_name", "")
    # Auto-discover namespace via find_workload for chat/react when not stated
    if ctx.surface in ("chat", "react") and not params.get("namespace") and svc_name:
        try:
            from k8s.wrappers import find_workload
            fw = find_workload(svc_name)
            svcs = fw.get("services", [])
            if svcs:
                ns = svcs[0].get("namespace") or ns
                svc_name = svcs[0].get("name") or svc_name
        except Exception:
            pass
    return get_service(
        ns,
        svc_name,
        ports_only=bool(params.get("ports_only", False)),
        selector_only=bool(params.get("selector_only", False)),
        traffic_policy_only=bool(params.get("traffic_policy_only", False)),
    )


def _handle_get_endpoints(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import get_endpoints
    return get_endpoints(
        params.get("namespace") or "default",
        params.get("service_name", ""),
        include_slices=bool(params.get("include_slices", True)),
    )


def _handle_get_rollout_status(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import get_rollout_status
    return get_rollout_status(params.get("namespace") or "default", params.get("deployment_name", ""))


def _handle_list_services(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import list_services
    return list_services(params.get("namespace") or "default")


def _handle_get_resource_graph(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import get_resource_graph
    return get_resource_graph(params.get("namespace") or "default")


def _handle_list_kubeconfig_contexts(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import list_kubeconfig_contexts
    return list_kubeconfig_contexts()


def _handle_switch_kubeconfig_context(params: dict, ctx: DispatchContext) -> dict:
    context_name = params["context_name"]

    if ctx.session_id:
        try:
            import db
            from k8s.kubectl_runner import KubectlRunner

            conn = db.get_cluster_connection(ctx.session_id)
            if not conn:
                return {
                    "success": False,
                    "error": (
                        "No cluster connection is associated with this session. "
                        "Connect or upload a kubeconfig before switching context."
                    ),
                    "context_name": context_name,
                }

            mode = conn.get("mode", "autodetect")
            kubeconfig_path = conn.get("kubeconfig_path")
            candidate = KubectlRunner(
                kubeconfig_path=kubeconfig_path,
                context=context_name,
            )
            check = candidate.run(["cluster-info"], max_output=1024 * 1024)
            if not check.success:
                return {
                    "success": False,
                    "error": check.stderr or "Context connectivity check failed",
                    "context_name": context_name,
                }

            namespace = "default"
            namespace_result = candidate.run(
                ["config", "view", "--minify", "--output", "jsonpath={.contexts[0].context.namespace}"],
                max_output=4096,
            )
            if namespace_result.success and namespace_result.stdout.strip():
                candidate_namespace = namespace_result.stdout.strip()
                if re.match(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", candidate_namespace):
                    namespace = candidate_namespace

            db.save_cluster_connection(
                session_id=ctx.session_id,
                mode=mode,
                context_name=context_name,
                cluster_name=context_name,
                server_url="",
                namespace=namespace,
                kubeconfig_path=kubeconfig_path,
            )
            return {
                "success": True,
                "context_name": context_name,
                "namespace": namespace,
                "message": f"Session context switched to '{context_name}'",
                "session_scoped": True,
            }
        except ImportError:
            logger.warning("Session-aware context switch requested but db module is unavailable")
            return {
                "success": False,
                "error": "Session-aware context switch is unavailable in this runtime.",
                "context_name": context_name,
            }
        except Exception as exc:
            logger.exception("Failed session-aware context switch")
            return {
                "success": False,
                "error": str(exc),
                "context_name": context_name,
            }

    # Standalone MCP/no-session fallback may still intentionally switch the
    # configured kubeconfig context. Chat sessions should always use the branch
    # above to avoid cross-user cluster misrouting.
    from k8s.wrappers import switch_kubeconfig_context
    return switch_kubeconfig_context(context_name)


def _handle_get_current_context(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import get_current_context
    return get_current_context()


def _handle_k8sgpt_analyze(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import k8sgpt_analyze
    return k8sgpt_analyze(params.get("namespace"), params.get("filter_text"))


# -- Kubeconfig management --

def _handle_add_kubeconfig_context(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import add_kubeconfig_context
    return add_kubeconfig_context(
        params["ssh_connection"],
        params.get("password"),
        params.get("context_name"),
        params.get("port", 22),
    )


# -- Deployment repo tools --

def _handle_search_deployment_repo(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import search_deployment_repo
    return search_deployment_repo(
        params["query"], params.get("path_filter"), params.get("file_extension"),
    )


def _handle_get_deployment_repo_file(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import get_deployment_repo_file
    return get_deployment_repo_file(params["file_path"])


def _handle_list_deployment_repo_path(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import list_deployment_repo_path
    return list_deployment_repo_path(params.get("path", ""))


# -- Write operations --

def _handle_exec_pod_command(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import exec_pod_command
    return exec_pod_command(
        params.get("namespace") or "default",
        params.get("pod_name", ""),
        params.get("command", ""),
        params.get("container"),
        params.get("confirm", False),
    )


def _handle_delete_pod(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import delete_pod
    return delete_pod(
        params.get("namespace") or "default",
        params.get("pod_name", ""),
        params.get("grace_period", 30),
        params.get("confirm", False),
        dry_run=params.get("dry_run", False),
        confirmation_token=params.get("confirmation_token"),
    )


def _handle_rollout_restart(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import rollout_restart
    return rollout_restart(
        params.get("namespace") or "default",
        params.get("deployment_name", ""),
        params.get("confirm", False),
        dry_run=params.get("dry_run", False),
        confirmation_token=params.get("confirmation_token"),
    )


def _handle_scale_deployment(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import scale_deployment
    return scale_deployment(
        params.get("namespace") or "default",
        params.get("deployment_name", ""),
        params.get("replicas", 1),
        params.get("confirm", False),
        dry_run=params.get("dry_run", False),
        confirmation_token=params.get("confirmation_token"),
    )


def _handle_apply_patch(params: dict, ctx: DispatchContext) -> dict:
    from k8s.wrappers import apply_patch
    return apply_patch(
        params.get("namespace") or "default",
        params.get("resource_type", ""),
        params.get("resource_name", ""),
        params.get("patch", ""),
        params.get("patch_type", "strategic"),
        params.get("confirm", False),
        dry_run=params.get("dry_run", False),
        confirmation_token=params.get("confirmation_token"),
    )


# -- Multi-step remediation plans (Feature C) --------------------------------

def _handle_propose_remediation_plan(params: dict, ctx: DispatchContext) -> dict:
    from services.plans import build_plan, plan_store, audit_plan_event, PlanValidationError
    try:
        plan = build_plan(
            issue=params.get("issue", ""),
            steps=params.get("steps", []),
            user=getattr(ctx, "user", None) or "anonymous",
            notes=params.get("notes") or "",
        )
    except PlanValidationError as e:
        return {"success": False, "error": str(e)}

    plan_store.put(plan)
    audit_plan_event("proposed", plan.plan_id, user=plan.user)
    return {"success": True, "plan": plan.to_dict()}


def _handle_get_plan(params: dict, ctx: DispatchContext) -> dict:
    from services.plans import plan_store
    plan = plan_store.get(params.get("plan_id", ""))
    if plan is None:
        return {"success": False, "error": "plan not found or expired"}
    return {"success": True, "plan": plan.to_dict()}


def _handle_execute_plan_step(params: dict, ctx: DispatchContext) -> dict:
    from services.plans import execute_step
    return execute_step(
        plan_id=params.get("plan_id", ""),
        step_index=int(params.get("step_index", 0)),
        confirmation_token=params.get("confirmation_token", ""),
    )


def _handle_kb_search(params: dict, ctx: DispatchContext) -> dict:
    """RAG knowledge-base search (Phase 1.2)."""
    from services.embeddings import embeddings as _emb
    from services.vector_db import vector_db as _vdb
    from services.rag.schema import DEVOPS_DOC as _DD, get_collection as _get_coll

    coll = (params.get("collection") or _DD.name).strip()
    if _get_coll(coll) is None:
        return {"success": False, "error": f"unknown collection '{coll}'"}

    try:
        _vdb.connect()
    except Exception as exc:
        return {"success": False, "error": f"vector DB unavailable: {exc}"}

    filters = {
        k: v for k, v in {
            "namespace": params.get("namespace"),
            "cluster":   params.get("cluster"),
            "kind":      params.get("kind"),
        }.items() if v is not None
    }
    if params.get("verified_only"):
        filters["verified"] = True

    qvec = _emb.embed(params.get("query", ""))
    hits = _vdb.search_in(
        collection=coll, query_vector=qvec,
        filters=filters or None, limit=int(params.get("limit", 5)),
    )
    return {
        "success": True, "collection": coll, "count": len(hits),
        "filters": filters, "results": hits,
    }


# -- AI analysis tools (return JSON strings; handler parses for chat/react) --

def _handle_analyze_error(params: dict, ctx: DispatchContext) -> dict:
    from ai_tools.analyze import run
    raw = run(
        params.get("error_text", ""),
        params.get("tool", "kubernetes"),
        params.get("environment", "production"),
        structured_payload=params.get("structured_payload"),
        diagnostic_mode=params.get("diagnostic_mode"),
    )
    if ctx.surface == "mcp":
        return {"_raw_text": raw}
    return json.loads(raw)


def _handle_get_fix_commands(params: dict, ctx: DispatchContext) -> dict:
    from ai_tools.fix import get_fix_commands
    raw = get_fix_commands(
        error_text=params.get("error_text"),
        category=params.get("category"),
    )
    if ctx.surface == "mcp":
        return {"_raw_text": raw}
    return json.loads(raw)


def _handle_list_error_categories(params: dict, ctx: DispatchContext) -> dict:
    from ai_tools.fix import list_categories
    raw = list_categories()
    if ctx.surface == "mcp":
        return {"_raw_text": raw}
    return json.loads(raw)


def _handle_cluster_report(params: dict, ctx: DispatchContext) -> dict:
    from ai_tools.report import cluster_report
    raw = cluster_report(params["events_text"])
    if ctx.surface == "mcp":
        return {"_raw_text": raw}
    return json.loads(raw)


def _handle_error_summary(params: dict, ctx: DispatchContext) -> dict:
    from ai_tools.report import error_summary
    raw = error_summary(params.get("errors", []))
    if ctx.surface == "mcp":
        return {"_raw_text": raw}
    return json.loads(raw)


def _handle_generate_runbook(params: dict, ctx: DispatchContext) -> dict:
    from ai_tools.runbook import generate_runbook
    raw = generate_runbook(
        error_text=params.get("error_text"),
        category=params.get("category"),
    )
    if ctx.surface == "mcp":
        return {"_raw_text": raw}
    return json.loads(raw)


# ── Schema imports ──────────────────────────────────────────────────────────

from mcp_server.schemas import (
    FindWorkloadInput, GetPodsInput, GetNodesInput, InvestigateNodeInput, GetNamespacesInput,
    ListNamespaceResourcesInput, ListServicesInput, GetResourceGraphInput,
    GetConfigMapInput, SearchConfigMapsInput,
    HelmAvailableInput, ListHelmReleasesInput, GetHelmReleaseInput, DiffHelmRevisionsInput,
    InvestigateHelmReleaseInput,
    DescribePodInput, GetPodLogsInput, GetEventsInput,
    GetDeploymentInput, GetServiceInput, GetEndpointsInput,
    GetRecentChangesInput,
    GetRolloutStatusInput, K8sgptAnalyzeInput,
    AddKubeconfigContextInput, ListKubeconfigContextsInput,
    SwitchKubeconfigContextInput, GetCurrentContextInput,
    SearchDeploymentRepoInput, GetDeploymentRepoFileInput,
    ListDeploymentRepoPathInput,
    InvestigatePodInput, InvestigateWorkloadInput, AnalyzeNamespaceInput,
    PromQueryInput,
    ExecPodCommandInput, DeletePodInput, RolloutRestartInput,
    ScaleDeploymentInput, ApplyPatchInput,
    AnalyzeErrorInput, GetFixCommandsInput, ListErrorCategoriesInput,
    ClusterReportInput, ErrorSummaryInput, GenerateRunbookInput,
    ProposeRemediationPlanInput, GetPlanInput, ExecutePlanStepInput,
    KbSearchInput,
)


# ── Convenience sets ────────────────────────────────────────────────────────

_ALL = frozenset({"mcp", "chat", "react"})
_MCP_ONLY = frozenset({"mcp"})
_MCP_CHAT_REACT = _ALL


# ── Tool registry ───────────────────────────────────────────────────────────

TOOLS: dict[str, ToolDef] = {}


def _reg(tool: ToolDef) -> None:
    """Register a tool (internal helper)."""
    TOOLS[tool.name] = tool


# -- Investigation tools --

_reg(ToolDef(
    name="investigate_pod",
    handler=_handle_investigate_pod,
    schema=InvestigatePodInput,
    description=(
        "Deep investigation of a specific pod: collects status, describe, logs, "
        "events, and AI analysis. Best first tool for 'why is X crashing?'"
    ),
    category="investigation",
    surfaces=_ALL,
))

_reg(ToolDef(
    name="investigate_workload",
    handler=_handle_investigate_workload,
    schema=InvestigateWorkloadInput,
    description=(
        "Investigate a deployment/statefulset/daemonset: replica status, pod health, "
        "rollout history, events, AI analysis."
    ),
    category="investigation",
    surfaces=_ALL,
    aliases=("describe_workload", "list_workload_pods"),
))

_reg(ToolDef(
    name="analyze_namespace",
    handler=_handle_analyze_namespace,
    schema=AnalyzeNamespaceInput,
    description="Holistic health check of an entire namespace: all pods, events, services, issues.",
    category="investigation",
    surfaces=_ALL,
))

# -- Discovery tools --

_reg(ToolDef(
    name="find_workload",
    handler=_handle_find_workload,
    schema=FindWorkloadInput,
    description=(
        "Search for matching workloads (deployments, pods, services) across all namespaces. "
        "Use when you know the name but not the namespace."
    ),
    category="discovery",
    surfaces=_ALL,
))

_reg(ToolDef(
    name="get_namespaces",
    handler=_handle_get_namespaces,
    schema=GetNamespacesInput,
    description="List all namespaces in the current cluster with status and labels.",
    category="discovery",
    surfaces=_ALL,
))

_reg(ToolDef(
    name="get_nodes",
    handler=_handle_get_nodes,
    schema=GetNodesInput,
    description=(
        "List all nodes in the cluster with status, roles, labels, taints, addresses, "
        "full conditions, capacity, and allocatable CPU/memory. Use this for cluster-wide "
        "node listing questions, including 'get all node labels for all nodes', taints, conditions, and addresses. "
        "For focused questions, set labels_only, taints_only, conditions_only, or "
        "addresses_only to true."
    ),
    category="discovery",
    surfaces=_ALL,
))

_reg(ToolDef(
    name="investigate_node",
    handler=_handle_investigate_node,
    schema=InvestigateNodeInput,
    description=(
        "Inspect a specific node's capacity, allocatable CPU/memory, current pod CPU "
        "requests/limits, and readiness conditions. Use for questions like 'what CPU "
        "is allocated on node X?'"
    ),
    category="investigation",
    surfaces=_ALL,
    aliases=("describe_node",),
))

_reg(ToolDef(
    name="list_namespace_resources",
    handler=_handle_list_namespace_resources,
    schema=ListNamespaceResourcesInput,
    description=(
        "Get an aggregate view of everything running in a namespace: pods, services, "
        "deployments, statefulsets, daemonsets, configmaps, PVCs, and ingresses. "
        "Includes safe labels, selectors, workload images, replica health, service ports, "
        "ingress backend paths, and PVC capacity without exposing ConfigMap data."
    ),
    category="discovery",
    surfaces=_ALL,
    aliases=("describe_pod_pvcs",),
))

_reg(ToolDef(
    name="search_configmaps",
    handler=_handle_search_configmaps,
    schema=SearchConfigMapsInput,
    description=(
        "Find which ConfigMap in a namespace contains a value or key. Read-only. "
        "Use when you know a failing value (e.g. a pinned dependency/plugin version "
        "or config string) but not where it is defined. Returns the owning ConfigMap "
        "name, key, and a redacted excerpt. Sensitive-looking values are redacted."
    ),
    category="discovery",
    surfaces=_ALL,
    react_enabled=True,
))

_reg(ToolDef(
    name="get_configmap",
    handler=_handle_get_configmap,
    schema=GetConfigMapInput,
    description=(
        "Read a single ConfigMap's data by name. Read-only. With a `key`, returns that "
        "key's redacted, size-capped value; without a key, returns the key list, "
        "labels/annotations (chart/managed-by/ArgoCD), and small previews — not full "
        "values. Secret values are out of scope and never returned here."
    ),
    category="discovery",
    surfaces=_ALL,
    react_enabled=True,
))

# -- Helm tools (read-only) --

_reg(ToolDef(
    name="helm_available",
    handler=_handle_helm_available,
    schema=HelmAvailableInput,
    description=(
        "Check whether Helm is installed and reachable on the active target "
        "(local or SSH). Returns availability and version. Read-only."
    ),
    category="helm",
    surfaces=_ALL,
    react_enabled=True,
))

_reg(ToolDef(
    name="list_helm_releases",
    handler=_handle_list_helm_releases,
    schema=ListHelmReleasesInput,
    description=(
        "List Helm releases in a namespace (or all namespaces only when explicitly "
        "requested). Optional status_filter "
        "(failed/pending/deployed/superseded/uninstalling). Returns name, namespace, "
        "revision, status, chart, app version, and updated time. Read-only."
    ),
    category="helm",
    surfaces=_ALL,
    react_enabled=True,
))

_reg(ToolDef(
    name="get_helm_release",
    handler=_handle_get_helm_release,
    schema=GetHelmReleaseInput,
    description=(
        "Read a named Helm release: status, history, and values by default; request "
        "manifest/hooks/notes/metadata sections only when needed (each an extra helm "
        "call). 'hooks' surfaces failed release hooks. Pass revision=N to read a past "
        "revision (use two calls to diff revisions). Values/manifests/hooks/notes are "
        "redacted and size-capped. Use to trace chart/values source for a workload. Read-only."
    ),
    category="helm",
    surfaces=_ALL,
    react_enabled=True,
))

_reg(ToolDef(
    name="diff_helm_revisions",
    handler=_handle_diff_helm_revisions,
    schema=DiffHelmRevisionsInput,
    description=(
        "Unified diff of two revisions of a Helm release's values (default) or "
        "rendered manifest. Use to answer 'what changed in the last upgrade?'. "
        "Both sides are redacted before diffing, so secret values never appear; "
        "this means changed=false is 'no non-secret changes' (a secret-only change "
        "is hidden, flagged by redaction_may_hide_secret_only_changes). Output is "
        "size-capped. Read-only."
    ),
    category="helm",
    surfaces=_ALL,
    react_enabled=True,
))

_reg(ToolDef(
    name="investigate_helm_release",
    handler=_handle_investigate_helm_release,
    schema=InvestigateHelmReleaseInput,
    description=(
        "Composite read-only investigation of a Helm release: status, recent "
        "revisions, the rendered resource list, plus live pod health and recent "
        "warning events, with a simple health assessment. Use for 'why is this "
        "Helm release unhealthy?'. Manifests are redacted before parsing. Read-only."
    ),
    category="helm",
    surfaces=_ALL,
    react_enabled=True,
))

# -- Pod tools --

_reg(ToolDef(
    name="get_pods",
    handler=_handle_get_pods,
    schema=GetPodsInput,
    description=(
        "List pods in a namespace with optional label selector and status filter. "
        "Use namespace='*' for all namespaces. For focused inventory questions, "
        "set labels_only, images_only, resources_only, or placement_only to true "
        "so the result contains only the requested pod fields."
    ),
    category="pod",
    surfaces=_ALL,
))

_reg(ToolDef(
    name="get_pod_logs",
    handler=_handle_get_pod_logs,
    schema=GetPodLogsInput,
    description=(
        "Get logs from a specific pod. Set previous=true for crashed container logs."
    ),
    category="pod",
    surfaces=_ALL,
))

_reg(ToolDef(
    name="describe_pod",
    handler=_handle_describe_pod,
    schema=DescribePodInput,
    description="Get full kubectl describe output for a pod.",
    category="pod",
    surfaces=_MCP_ONLY,
    react_enabled=False,
    notes="Used internally by investigate_pod; not exposed to chat/react directly.",
))

# -- Cluster state tools --

_reg(ToolDef(
    name="get_events",
    handler=_handle_get_events,
    schema=GetEventsInput,
    description=(
        "Get events in a namespace. Use namespace='*' for all. "
        "Use field_selector='type=Warning' for warnings only."
    ),
    category="cluster",
    surfaces=_ALL,
))


# -- Metrics --

def _handle_prom_query(params: dict, ctx: DispatchContext) -> Any:
    """Run an instant PromQL query. Fails soft when PROMETHEUS_URL is not
    configured (returns a `{"unavailable": True, ...}` payload) so a single
    Prometheus outage cannot abort an investigation that requested metrics
    evidence."""
    from services.prometheus import query as prom_query
    return prom_query(params.get("query", ""))


_reg(ToolDef(
    name="prom_query",
    handler=_handle_prom_query,
    schema=PromQueryInput,
    description=(
        "Run an instant PromQL query against the configured Prometheus. "
        "Returns the standard `data.resultType`/`data.result` payload from "
        "/api/v1/query. Fails soft (returns unavailable:true) when "
        "Prometheus is not configured or unreachable — investigations still "
        "complete without metrics evidence."
    ),
    category="metrics",
    surfaces=_ALL,
))


_reg(ToolDef(
    name="get_deployment",
    handler=_handle_get_deployment,
    schema=GetDeploymentInput,
    description=(
        "Get details of a specific deployment, including replica health, selector, labels, "
        "rollout metadata, and safe pod template summary. For focused questions, set "
        "labels_only, images_only, resources_only, or template_only to true."
    ),
    category="cluster",
    surfaces=_ALL,
))

_reg(ToolDef(
    name="get_service",
    handler=_handle_get_service,
    schema=GetServiceInput,
    description=(
        "Get details of a specific service including labels, safe annotation keys, "
        "selectors, structured ports, load balancer status, traffic policies, "
        "IP families, and session affinity. For focused questions, set ports_only, "
        "selector_only, or traffic_policy_only to true."
    ),
    category="cluster",
    surfaces=_ALL,
))

_reg(ToolDef(
    name="get_endpoints",
    handler=_handle_get_endpoints,
    schema=GetEndpointsInput,
    description=(
        "Check endpoints for a service. Includes legacy Endpoints plus EndpointSlice "
        "readiness when available: ready, serving, terminating, targetRef, nodeName, "
        "zone/topology hints, and ports."
    ),
    category="cluster",
    surfaces=_ALL,
))

_reg(ToolDef(
    name="get_recent_changes",
    handler=_handle_get_recent_changes,
    schema=GetRecentChangesInput,
    description=(
        "Find what was deployed or changed in a namespace recently, and what "
        "the change was. Reach for this FIRST when something broke suddenly: "
        "a rollout minutes before an alert is the most common cause, and "
        "'nothing changed' rules it out. Returns each workload that rolled "
        "out inside the window with its revision, per-container image diff, "
        "and change-cause when one was recorded."
    ),
    category="investigation",
    surfaces=_ALL,
))

_reg(ToolDef(
    name="get_rollout_status",
    handler=_handle_get_rollout_status,
    schema=GetRolloutStatusInput,
    description="Check if a deployment rollout is progressing.",
    category="cluster",
    surfaces=_ALL,
))

_reg(ToolDef(
    name="list_services",
    handler=_handle_list_services,
    schema=ListServicesInput,
    description="List all services in a namespace with type, cluster IP, ports, and selectors.",
    category="cluster",
    surfaces=_ALL,
))

_reg(ToolDef(
    name="get_resource_graph",
    handler=_handle_get_resource_graph,
    schema=GetResourceGraphInput,
    description=(
        "Build a visual resource graph for a namespace. Returns nodes "
        "(ingresses, services, deployments, pods with status) and edges."
    ),
    category="cluster",
    surfaces=_ALL,
))

_reg(ToolDef(
    name="list_kubeconfig_contexts",
    handler=_handle_list_kubeconfig_contexts,
    schema=ListKubeconfigContextsInput,
    description="List available kubeconfig contexts and show the active context.",
    category="cluster",
    surfaces=_ALL,
    aliases=("list_contexts",),
))

_reg(ToolDef(
    name="switch_kubeconfig_context",
    handler=_handle_switch_kubeconfig_context,
    schema=SwitchKubeconfigContextInput,
    description="Switch to a different cluster context.",
    category="cluster",
    surfaces=_ALL,
    aliases=("switch_context",),
))

_reg(ToolDef(
    name="get_current_context",
    handler=_handle_get_current_context,
    schema=GetCurrentContextInput,
    description="Get the currently active kubeconfig context.",
    category="cluster",
    surfaces=_MCP_ONLY,
    react_enabled=False,
    notes="Available in MCP; chat/react use list_contexts instead.",
))

_reg(ToolDef(
    name="k8sgpt_analyze",
    handler=_handle_k8sgpt_analyze,
    schema=K8sgptAnalyzeInput,
    description="Run k8sgpt analysis on the cluster or a specific namespace.",
    category="cluster",
    surfaces=_MCP_ONLY,
    react_enabled=False,
    notes="Requires k8sgpt binary. MCP-only for now.",
))

# -- Kubeconfig management --

_reg(ToolDef(
    name="add_kubeconfig_context",
    handler=_handle_add_kubeconfig_context,
    schema=AddKubeconfigContextInput,
    description="Add a new kubeconfig context via SSH connection.",
    category="cluster",
    surfaces=_MCP_ONLY,
    react_enabled=False,
))

# -- Deployment repo tools --

_reg(ToolDef(
    name="search_deployment_repo",
    handler=_handle_search_deployment_repo,
    schema=SearchDeploymentRepoInput,
    description="Search the deployment-provisioning repo for files matching a query.",
    category="repo",
    surfaces=_MCP_ONLY,
    react_enabled=False,
))

_reg(ToolDef(
    name="get_deployment_repo_file",
    handler=_handle_get_deployment_repo_file,
    schema=GetDeploymentRepoFileInput,
    description="Read a file from the deployment-provisioning repo.",
    category="repo",
    surfaces=_MCP_ONLY,
    react_enabled=False,
))

_reg(ToolDef(
    name="list_deployment_repo_path",
    handler=_handle_list_deployment_repo_path,
    schema=ListDeploymentRepoPathInput,
    description="List files in a directory of the deployment-provisioning repo.",
    category="repo",
    surfaces=_MCP_ONLY,
    react_enabled=False,
))

# -- Write operations --

_reg(ToolDef(
    name="exec_pod_command",
    handler=_handle_exec_pod_command,
    schema=ExecPodCommandInput,
    description="Execute a command inside a running pod.",
    category="write",
    surfaces=_MCP_ONLY,
    write_op=True,
    requires_confirm=True,
    react_enabled=False,
))

_reg(ToolDef(
    name="delete_pod",
    handler=_handle_delete_pod,
    schema=DeletePodInput,
    description="Delete a pod (it will be recreated by its controller).",
    category="write",
    surfaces=_MCP_ONLY,
    write_op=True,
    requires_confirm=True,
    react_enabled=False,
))

_reg(ToolDef(
    name="rollout_restart",
    handler=_handle_rollout_restart,
    schema=RolloutRestartInput,
    description="Perform a rolling restart of a deployment.",
    category="write",
    surfaces=_MCP_ONLY,
    write_op=True,
    requires_confirm=True,
    react_enabled=False,
))

_reg(ToolDef(
    name="scale_deployment",
    handler=_handle_scale_deployment,
    schema=ScaleDeploymentInput,
    description="Scale a deployment to a target replica count.",
    category="write",
    surfaces=_MCP_ONLY,
    write_op=True,
    requires_confirm=True,
    react_enabled=False,
))

_reg(ToolDef(
    name="apply_patch",
    handler=_handle_apply_patch,
    schema=ApplyPatchInput,
    description="Apply a JSON patch to a Kubernetes resource.",
    category="write",
    surfaces=_MCP_ONLY,
    write_op=True,
    requires_confirm=True,
    react_enabled=False,
))

# -- Multi-step remediation plans (Feature C) --

_reg(ToolDef(
    name="propose_remediation_plan",
    handler=_handle_propose_remediation_plan,
    schema=ProposeRemediationPlanInput,
    description=(
        "Propose a multi-step remediation plan. Each step is one of the "
        "allow-listed destructive tools (delete_pod, rollout_restart, "
        "scale_deployment, apply_patch). Returns a plan_id; step execution "
        "still requires dry-run + confirmation_token per step."
    ),
    category="plan",
    surfaces=_MCP_ONLY,
    write_op=False,
    requires_confirm=False,
    react_enabled=False,
))

_reg(ToolDef(
    name="get_plan",
    handler=_handle_get_plan,
    schema=GetPlanInput,
    description="Retrieve a stored remediation plan by id.",
    category="plan",
    surfaces=_MCP_ONLY,
    react_enabled=False,
))

_reg(ToolDef(
    name="execute_plan_step",
    handler=_handle_execute_plan_step,
    schema=ExecutePlanStepInput,
    description=(
        "Execute one step of a stored plan. Caller must first call the "
        "underlying destructive tool with dry_run=True to obtain the "
        "confirmation_token for that specific step."
    ),
    category="plan",
    surfaces=_MCP_ONLY,
    write_op=True,
    requires_confirm=True,
    react_enabled=False,
))

# -- Knowledge base search (Phase 1.2) --

_reg(ToolDef(
    name="kb_search",
    handler=_handle_kb_search,
    schema=KbSearchInput,
    description=(
        "Search the ingested RAG knowledge base (team docs, runbooks, "
        "captured chat resolutions, seeded errors) by semantic similarity. "
        "Returns top-N chunks with citations."
    ),
    category="rag",
    surfaces=_ALL,
    react_enabled=True,
))

# -- AI analysis tools --

_reg(ToolDef(
    name="analyze_error",
    handler=_handle_analyze_error,
    schema=AnalyzeErrorInput,
    description="AI diagnosis of a pasted error message or log snippet.",
    category="ai",
    surfaces=_ALL,
    returns_json_string=True,
))

_reg(ToolDef(
    name="get_fix_commands",
    handler=_handle_get_fix_commands,
    schema=GetFixCommandsInput,
    description="Get specific kubectl fix commands for an error.",
    category="ai",
    surfaces=_ALL,
    returns_json_string=True,
))

_reg(ToolDef(
    name="list_error_categories",
    handler=_handle_list_error_categories,
    schema=ListErrorCategoriesInput,
    description="List all known error categories with descriptions.",
    category="ai",
    surfaces=_MCP_ONLY,
    returns_json_string=True,
    react_enabled=False,
))

_reg(ToolDef(
    name="cluster_report",
    handler=_handle_cluster_report,
    schema=ClusterReportInput,
    description="Generate a cluster health report from events data.",
    category="ai",
    surfaces=_ALL,
    returns_json_string=True,
))

_reg(ToolDef(
    name="error_summary",
    handler=_handle_error_summary,
    schema=ErrorSummaryInput,
    description="Summarize multiple errors into a concise report.",
    category="ai",
    surfaces=_ALL,
    returns_json_string=True,
))

_reg(ToolDef(
    name="generate_runbook",
    handler=_handle_generate_runbook,
    schema=GenerateRunbookInput,
    description=(
        "Generate a step-by-step runbook for a recurring error. "
        "Only use when the user explicitly asks for a runbook."
    ),
    category="ai",
    surfaces=_ALL,
    returns_json_string=True,
))
# ── Alerts Database Tools ────────────────────────────────────────────────────
from alerts.api.mcp_tools import GetRecentAlertsParams, GetInvestigationDetailsParams
from alerts.api.mcp_tools import handle_get_recent_alerts, handle_get_investigation_details

_reg(ToolDef(
    name="get_recent_alerts",
    handler=handle_get_recent_alerts,
    schema=GetRecentAlertsParams,
    description="Get recent alerts from the alerts database. Useful to see what incidents occurred recently.",
    category="investigation",
    surfaces=_ALL,
    returns_json_string=False,
))

_reg(ToolDef(
    name="get_investigation_details",
    handler=handle_get_investigation_details,
    schema=GetInvestigationDetailsParams,
    description="Get the full JSON document of a specific investigation including RCA, evidence, and tool executions.",
    category="investigation",
    surfaces=_ALL,
    returns_json_string=False,
))

# ── Public API ──────────────────────────────────────────────────────────────

def resolve_tool(name: str) -> Optional[ToolDef]:
    """Look up a tool by canonical name or alias, with case-resilient and synonym matching."""
    if not name:
        return None

    # 1. Exact canonical name match
    tool = TOOLS.get(name)
    if tool:
        return tool

    # 2. Normalize and check again
    # Convert camelCase to snake_case, spaces/hyphens to underscores, lowercase all
    normalized = name.strip()
    # Handle camelCase -> snake_case
    normalized = re.sub(r'(?<!^)(?=[A-Z])', '_', normalized)
    # Replace hyphens or spaces with underscores, and lowercase
    normalized = re.sub(r'[- ]+', '_', normalized).lower()

    tool = TOOLS.get(normalized)
    if tool:
        return tool

    # 3. Check aliases (exact and normalized)
    for t in TOOLS.values():
        if name in t.aliases or normalized in t.aliases:
            return t

    # 4. Explicit safe synonym mapping (no fuzzy/edit-distance matching)
    synonyms = {
        "list_pods": "get_pods",
        "list_pod": "get_pods",
        "get_pod": "get_pods",
        "list_namespaces": "get_namespaces",
        "list_namespace": "get_namespaces",
        "get_namespace": "get_namespaces",
        "list_nodes": "get_nodes",
        "list_node": "get_nodes",
        "get_node": "get_nodes",
        "get_services": "list_services",
        "describe_node": "investigate_node",
        "describe_nodes": "investigate_node",
        "describe_deployment": "get_deployment",
        "describe_deployments": "get_deployment",
    }

    mapped_name = synonyms.get(normalized)
    if mapped_name:
        tool = TOOLS.get(mapped_name)
        if tool:
            return tool

    return None


def tools_for_surface(surface: ToolSurface) -> list[ToolDef]:
    """Return all tools available on a given surface, sorted by name."""
    return sorted(
        (t for t in TOOLS.values() if surface in t.surfaces),
        key=lambda t: t.name,
    )


def build_react_tool_descriptions(allowed_tools: Optional[Iterable[str]] = None) -> str:
    """Generate the TOOL_DESCRIPTIONS string from registry entries.

    Groups tools by category, filters to react-enabled tools on the
    'react' surface. Output format matches the existing freeform text
    in react.py so ReAct prompt behavior is preserved.

    If ``allowed_tools`` is provided, the description list is further filtered
    to that set — used by harness Phase 7 dynamic tool scoping. When None,
    every react-enabled tool is shown (legacy behavior).
    """
    allowed_set = set(allowed_tools) if allowed_tools is not None else None
    react_tools = [
        t for t in tools_for_surface("react")
        if t.react_enabled and (allowed_set is None or t.name in allowed_set)
    ]

    # Group by category, preserving a stable order.
    category_order = [
        "investigation", "discovery", "pod", "cluster", "helm", "rag", "ai",
    ]
    groups: dict[str, list[ToolDef]] = {}
    for t in react_tools:
        groups.setdefault(t.category, []).append(t)

    category_labels = {
        "investigation": "INVESTIGATION TOOLS (start here for debugging questions)",
        "discovery": "DISCOVERY TOOLS (use when you need to find things)",
        "pod": "POD TOOLS",
        "cluster": "CLUSTER STATE TOOLS",
        "helm": "HELM TOOLS (read-only release inspection)",
        "rag": "KNOWLEDGE BASE TOOLS",
        "ai": "AI ANALYSIS TOOLS (use after gathering data, or for specific requests)",
    }

    # Defensive: emit any react-enabled category not in category_order at the end,
    # so a newly added category can never be silently dropped from the prompt.
    ordered_cats = category_order + [c for c in groups if c not in category_order]

    lines = ["Available tools (call exactly one per step):\n"]
    for cat in ordered_cats:
        tools_in_cat = groups.get(cat, [])
        if not tools_in_cat:
            continue
        label = category_labels.get(cat, cat.upper())
        lines.append(f"{label}:")
        for t in tools_in_cat:
            signature = _schema_signature(t.schema)
            aliases = f" (aliases: {', '.join(t.aliases)})" if t.aliases else ""
            lines.append(f"- {t.name}({signature}){aliases} -- {t.description}")
        lines.append("")

    return "\n".join(lines)


_PARAM_ALIASES: dict[str, dict[str, str]] = {
    "investigate_node": {"name": "node_name", "nodeName": "node_name", "node": "node_name"},
    "get_nodes": {
        "name": "node_name",
        "nodeName": "node_name",
        "node": "node_name",
        "labelsOnly": "labels_only",
        "only_labels": "labels_only",
        "taintsOnly": "taints_only",
        "only_taints": "taints_only",
        "conditionsOnly": "conditions_only",
        "only_conditions": "conditions_only",
        "addressesOnly": "addresses_only",
        "only_addresses": "addresses_only",
    },
    "investigate_pod": {"name": "pod_name", "pod": "pod_name", "podName": "pod_name"},
    "get_pods": {
        "labelsOnly": "labels_only",
        "only_labels": "labels_only",
        "imagesOnly": "images_only",
        "only_images": "images_only",
        "resourcesOnly": "resources_only",
        "only_resources": "resources_only",
        "placementOnly": "placement_only",
        "only_placement": "placement_only",
    },
    "get_pod_logs": {"name": "pod_name", "pod": "pod_name", "podName": "pod_name"},
    "describe_pod": {"name": "pod_name", "pod": "pod_name", "podName": "pod_name"},
    "get_deployment": {
        "name": "deployment_name",
        "deployment": "deployment_name",
        "deploymentName": "deployment_name",
        "labelsOnly": "labels_only",
        "only_labels": "labels_only",
        "imagesOnly": "images_only",
        "only_images": "images_only",
        "resourcesOnly": "resources_only",
        "only_resources": "resources_only",
        "templateOnly": "template_only",
        "only_template": "template_only",
    },
    "get_rollout_status": {"name": "deployment_name", "deployment": "deployment_name", "deploymentName": "deployment_name"},
    "get_service": {
        "name": "service_name",
        "service": "service_name",
        "serviceName": "service_name",
        "portsOnly": "ports_only",
        "only_ports": "ports_only",
        "selectorOnly": "selector_only",
        "only_selector": "selector_only",
        "trafficPolicyOnly": "traffic_policy_only",
        "only_traffic_policy": "traffic_policy_only",
    },
    "get_endpoints": {
        "name": "service_name",
        "service": "service_name",
        "serviceName": "service_name",
        "includeSlices": "include_slices",
    },
    "investigate_workload": {"name": "workload_name", "workload": "workload_name", "workloadName": "workload_name", "kind": "workload_type"},
    "analyze_error": {"error": "error_text", "message": "error_text", "log": "error_text"},
    "get_fix_commands": {"error": "error_text", "message": "error_text", "log": "error_text"},
    "cluster_report": {"events": "events_text"},
}


def _schema_signature(schema: type) -> str:
    """Return a compact, prompt-friendly parameter signature."""
    try:
        js = schema.model_json_schema()
    except Exception:
        return ""
    props = js.get("properties") or {}
    required = set(js.get("required") or [])
    parts: list[str] = []
    for name in props:
        suffix = "" if name in required else "?"
        parts.append(f"{name}{suffix}")
    return ", ".join(parts)


def _normalize_params(tool: ToolDef, params: dict | None) -> dict:
    """Apply tool-specific aliases and small type coercions before validation."""
    normalized = dict(params or {})
    aliases = _PARAM_ALIASES.get(tool.name, {})
    for source, target in aliases.items():
        if source in normalized and target not in normalized:
            normalized[target] = normalized[source]
    for key, value in list(normalized.items()):
        if isinstance(value, str):
            low = value.strip().lower()
            if low == "true":
                normalized[key] = True
            elif low == "false":
                normalized[key] = False
    return normalized


def _validate_params(tool: ToolDef, params: dict) -> tuple[dict | None, dict | None]:
    """Validate params against the tool schema. Returns (params, error)."""
    try:
        model = tool.schema.model_validate(params)
        return model.model_dump(exclude_none=True), None
    except Exception as exc:
        details = None
        if hasattr(exc, "errors"):
            try:
                details = exc.errors()
            except Exception:
                details = None
        try:
            expected = tool.schema.model_json_schema()
        except Exception:
            expected = {}
        return None, {
            "error": "invalid_params",
            "tool": tool.name,
            "message": f"Invalid parameters for tool '{tool.name}'",
            "details": details or str(exc),
            "expected_schema": expected,
        }


def valid_tool_names(surface: ToolSurface = "react") -> list[str]:
    """Return valid tool names and aliases for a surface, for LLM recovery hints."""
    names: list[str] = []
    for tool in tools_for_surface(surface):
        if surface == "react" and not tool.react_enabled:
            continue
        names.append(tool.name)
        names.extend(tool.aliases)
    return sorted(set(names))


def dispatch(tool_name: str, params: dict, ctx: DispatchContext) -> dict:
    """Shared dispatcher: resolve tool, validate surface, call handler.

    This preserves the error handling behavior from chat.py _dispatch:
    - Not-found errors trigger cross-namespace search via find_workload
    - Generic errors are returned as structured dicts
    """
    import time
    start_time = time.perf_counter()

    tool = resolve_tool(tool_name)
    if tool is None:
        err_res = {
            "error": "unknown_tool",
            "message": f"Unknown tool: {tool_name}",
            "tool": tool_name,
            "valid_tools": valid_tool_names(ctx.surface if ctx.surface != "playbook" else "react"),
        }
        if ctx.surface == "react":
            from services.tool_envelope import make_generic_envelope
            duration_ms = (time.perf_counter() - start_time) * 1000
            return make_generic_envelope(tool_name, err_res, params or {}, duration_ms)
        return err_res

    # Playbook surface has access to all tools (it orchestrates investigation steps)
    if ctx.surface != "playbook" and ctx.surface not in tool.surfaces:
        err_res = {
            "error": "tool_unavailable",
            "message": f"Tool '{tool.name}' is not available on {ctx.surface}",
            "tool": tool.name,
            "valid_tools": valid_tool_names(ctx.surface),
        }
        if ctx.surface == "react":
            from services.tool_envelope import make_generic_envelope
            duration_ms = (time.perf_counter() - start_time) * 1000
            return make_generic_envelope(tool.name, err_res, params or {}, duration_ms)
        return err_res

    normalized = _normalize_params(tool, params)
    validated, validation_error = _validate_params(tool, normalized)
    if validation_error is not None:
        if ctx.surface == "react":
            from services.tool_envelope import make_generic_envelope
            duration_ms = (time.perf_counter() - start_time) * 1000
            return make_generic_envelope(tool.name, validation_error, normalized or {}, duration_ms)
        return validation_error

    try:
        # Handlers conditionally wrap their result in a ToolEnvelope when
        # ctx.surface == "react"; all other surfaces (mcp, chat single-shot,
        # rest, playbook) receive the legacy raw shape. Dispatch passes the
        # result through unchanged regardless of type.
        result = tool.handler(validated or {}, ctx)
        if ctx.surface == "react":
            from services.tool_envelope import ToolEnvelope, make_generic_envelope
            if not isinstance(result, ToolEnvelope):
                duration_ms = (time.perf_counter() - start_time) * 1000
                result = make_generic_envelope(tool.name, result, validated or {}, duration_ms)
        return result
    except Exception as exc:
        err_msg = str(exc)
        logger.exception("dispatch error for %s", tool.name)

        # Not-found fallback: search across namespaces
        not_found = (
            "not found" in err_msg.lower()
            or "notfound" in err_msg.lower()
            or "no resources found" in err_msg.lower()
        )
        if not_found:
            name = (
                normalized.get("deployment_name")
                or normalized.get("service_name")
                or normalized.get("pod_name")
                or normalized.get("node_name")
                or normalized.get("name")
                or ""
            )
            ns = normalized.get("namespace", "default")
            if name:
                try:
                    from k8s.wrappers import find_workload
                    fw_result = find_workload(name)
                    fw_result["_not_found_hint"] = (
                        f"'{name}' was not found in namespace '{ns}'. "
                        f"Searched across all namespaces instead:"
                    )
                    if ctx.surface == "react":
                        from services.tool_envelope import make_generic_envelope
                        duration_ms = (time.perf_counter() - start_time) * 1000
                        return make_generic_envelope(tool.name, fw_result, validated or {}, duration_ms)
                    return fw_result
                except Exception:
                    pass
            err_res = {
                "error": err_msg,
                "suggestion": (
                    f"The resource was not found in namespace '{ns}'. "
                    "Try specifying the namespace explicitly."
                ),
            }
            if ctx.surface == "react":
                from services.tool_envelope import make_generic_envelope
                duration_ms = (time.perf_counter() - start_time) * 1000
                return make_generic_envelope(tool.name, err_res, validated or {}, duration_ms)
            return err_res

        err_res = {"error": err_msg, "tool": tool.name}
        if ctx.surface == "react":
            from services.tool_envelope import make_generic_envelope
            duration_ms = (time.perf_counter() - start_time) * 1000
            return make_generic_envelope(tool.name, err_res, validated or {}, duration_ms)
        return err_res
