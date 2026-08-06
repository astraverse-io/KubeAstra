"""Regression tests for the KubeAstra tool registry."""

from pathlib import Path
import sys

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from tool_registry import (  # noqa: E402
    TOOLS,
    build_react_tool_descriptions,
    resolve_tool,
    tools_for_surface,
)


EXPECTED_TOOLS = {
    "add_kubeconfig_context",
    "analyze_error",
    "analyze_namespace",
    "apply_patch",
    "cluster_report",
    "delete_pod",
    "describe_pod",
    "diff_helm_revisions",
    "error_summary",
    "exec_pod_command",
    "execute_plan_step",
    "find_workload",
    "generate_runbook",
    "get_configmap",
    "get_current_context",
    "get_deployment",
    "get_deployment_repo_file",
    "get_endpoints",
    "get_events",
    "get_fix_commands",
    "get_namespaces",
    "get_nodes",
    "get_plan",
    "get_helm_release",
    "get_investigation_details",
    "get_pod_logs",
    "get_pods",
    "get_recent_alerts",
    "get_recent_changes",
    "get_resource_graph",
    "get_rollout_status",
    "get_service",
    "helm_available",
    "investigate_helm_release",
    "investigate_pod",
    "investigate_node",
    "investigate_workload",
    "k8sgpt_analyze",
    "kb_search",
    "list_deployment_repo_path",
    "list_helm_releases",
    "list_error_categories",
    "list_kubeconfig_contexts",
    "list_namespace_resources",
    "list_services",
    "prom_query",
    "propose_remediation_plan",
    "rollout_restart",
    "scale_deployment",
    "search_configmaps",
    "search_deployment_repo",
    "switch_kubeconfig_context",
}

EXPECTED_CHAT_TOOLS = {
    "analyze_error",
    "analyze_namespace",
    "cluster_report",
    "diff_helm_revisions",
    "error_summary",
    "find_workload",
    "generate_runbook",
    "get_configmap",
    "get_deployment",
    "get_endpoints",
    "get_events",
    "get_fix_commands",
    "get_namespaces",
    "get_nodes",
    "get_helm_release",
    "get_investigation_details",
    "get_pod_logs",
    "get_pods",
    "get_recent_alerts",
    "get_recent_changes",
    "get_resource_graph",
    "get_rollout_status",
    "get_service",
    "helm_available",
    "investigate_helm_release",
    "investigate_pod",
    "investigate_node",
    "investigate_workload",
    "kb_search",
    "list_helm_releases",
    "list_kubeconfig_contexts",
    "list_namespace_resources",
    "list_services",
    "prom_query",
    "search_configmaps",
    "switch_kubeconfig_context",
}


def test_expected_tools_are_registered():
    assert set(TOOLS) == EXPECTED_TOOLS


def test_tool_metadata_is_complete():
    for name, tool in TOOLS.items():
        assert tool.name == name
        assert tool.handler is not None
        assert tool.schema is not None
        assert tool.description
        assert tool.category
        assert tool.surfaces


def test_aliases_resolve():
    assert resolve_tool("list_contexts").name == "list_kubeconfig_contexts"
    assert resolve_tool("switch_context").name == "switch_kubeconfig_context"
    assert resolve_tool("describe_node").name == "investigate_node"
    assert resolve_tool("describe_workload").name == "investigate_workload"
    assert resolve_tool("list_workload_pods").name == "investigate_workload"
    assert resolve_tool("describe_pod_pvcs").name == "list_namespace_resources"
    assert resolve_tool("does_not_exist") is None


def test_surface_filtering():
    assert {tool.name for tool in tools_for_surface("mcp")} == EXPECTED_TOOLS
    assert {tool.name for tool in tools_for_surface("chat")} == EXPECTED_CHAT_TOOLS
    assert {tool.name for tool in tools_for_surface("react")} == EXPECTED_CHAT_TOOLS


def test_write_tools_require_confirm_and_are_not_react_enabled():
    write_tools = [tool for tool in TOOLS.values() if tool.write_op]
    assert {tool.name for tool in write_tools} == {
        "apply_patch",
        "delete_pod",
        "exec_pod_command",
        "execute_plan_step",
        "rollout_restart",
        "scale_deployment",
    }
    for tool in write_tools:
        assert tool.requires_confirm
        assert not tool.react_enabled
        assert "react" not in tool.surfaces


def test_get_pods_schema_tracks_status_filter():
    assert "status_filter" in resolve_tool("get_pods").schema.model_fields


def test_react_descriptions_include_aliases_and_exclude_writes():
    descriptions = build_react_tool_descriptions()
    assert "investigate_pod" in descriptions
    assert "get_pods" in descriptions
    assert "list_contexts" in descriptions
    assert "switch_context" in descriptions
    assert "delete_pod" not in descriptions
    assert "rollout_restart" not in descriptions


def test_all_react_enabled_tools_appear_in_descriptions():
    # Guards against a tool being react_enabled but silently dropped from the
    # prompt because its category is not in build_react_tool_descriptions'
    # category_order (the helm tools hit exactly this bug).
    descriptions = build_react_tool_descriptions()
    react_tools = [t for t in tools_for_surface("react") if t.react_enabled]
    missing = [t.name for t in react_tools if t.name not in descriptions]
    assert not missing, f"react-enabled tools missing from prompt: {missing}"


def test_helm_tools_in_react_descriptions():
    descriptions = build_react_tool_descriptions()
    for name in ("helm_available", "list_helm_releases", "get_helm_release"):
        assert name in descriptions
