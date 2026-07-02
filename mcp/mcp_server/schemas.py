"""Pydantic input schemas for the MCP tools.

One schema class per tool, spanning the live kubectl, Helm, ConfigMap, RAG, plan,
and AI-analysis tool families. The authoritative tool set and counts live in
``tool_registry.py`` (the single source of truth); this module intentionally
avoids a hard-coded count so it cannot drift.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class FindWorkloadInput(BaseModel):
    """Input schema for find_workload tool."""
    
    name: str = Field(
        description="Workload name or partial name to search for"
    )
    environment: Optional[str] = Field(
        default=None,
        description="Environment hint (e.g., 'prod', 'staging', 'dev') to prioritize namespace search"
    )


class GetPodsInput(BaseModel):
    """Input schema for get_pods tool."""
    
    namespace: Optional[str] = Field(
        default="default",
        description="Namespace to query for pods"
    )
    label_selector: Optional[str] = Field(
        default=None,
        description="Optional label selector (e.g., 'app=myapp,env=prod')"
    )
    status_filter: Optional[str] = Field(
        default=None,
        description="Optional pod status filter (e.g., 'CrashLoopBackOff', 'ImagePullBackOff')"
    )
    exclude_namespaces: Optional[List[str]] = Field(
        default=None,
        description="Optional exact namespaces to exclude when namespace='*'"
    )
    exclude_namespace_prefixes: Optional[List[str]] = Field(
        default=None,
        description="Optional namespace prefixes to exclude when namespace='*' (e.g., ['kube-'])"
    )
    labels_only: bool = Field(
        default=False,
        description="Return only pod name, namespace, labels, and label_count"
    )
    images_only: bool = Field(
        default=False,
        description="Return only pod name, namespace, images, and container image mapping"
    )
    resources_only: bool = Field(
        default=False,
        description="Return only pod name, namespace, and container resource requests/limits"
    )
    placement_only: bool = Field(
        default=False,
        description="Return only pod name, namespace, node placement, and scheduling fields"
    )
    details: bool = Field(
        default=False,
        description="Use JSON-backed safe pod summaries for all-namespace requests"
    )


class GetNamespacesInput(BaseModel):
    """Input schema for get_namespaces tool."""

    pass


class GetNodesInput(BaseModel):
    """Input schema for get_nodes tool."""

    node_name: Optional[str] = Field(
        default=None,
        description=(
            "Optional node name. Leave empty to list all nodes with status, roles, "
            "capacity, allocatable resources, and labels. When provided, returns "
            "detailed allocated resources for that single node."
        )
    )
    labels_only: bool = Field(
        default=False,
        description=(
            "When true and node_name is empty, return only node name, labels, "
            "and label_count for each node."
        )
    )
    taints_only: bool = Field(
        default=False,
        description="When true and node_name is empty, return only node taints and unschedulable state"
    )
    conditions_only: bool = Field(
        default=False,
        description="When true and node_name is empty, return only full node conditions"
    )
    addresses_only: bool = Field(
        default=False,
        description="When true and node_name is empty, return only node addresses"
    )


class InvestigateNodeInput(BaseModel):
    """Input schema for investigate_node tool."""

    node_name: str = Field(
        description="Name of the Kubernetes node to inspect"
    )


class ListNamespaceResourcesInput(BaseModel):
    """Input schema for list_namespace_resources tool."""

    namespace: Optional[str] = Field(
        default="default",
        description="Namespace to inspect for aggregate resources"
    )


class GetConfigMapInput(BaseModel):
    """Input schema for get_configmap tool."""

    namespace: str = Field(description="Namespace of the ConfigMap")
    name: str = Field(description="ConfigMap name")
    key: Optional[str] = Field(
        default=None,
        description="Optional data key to read. Omit to get keys + small previews only.",
    )


class SearchConfigMapsInput(BaseModel):
    """Input schema for search_configmaps tool."""

    namespace: str = Field(description="Namespace to search")
    query: str = Field(
        description="Value or key substring to find (e.g. a pinned version like '2.2277.v00573e73ddf1')"
    )
    max_matches: Optional[int] = Field(
        default=20, description="Maximum number of matches to return"
    )


class HelmAvailableInput(BaseModel):
    """Input schema for helm_available tool (no parameters)."""


class ListHelmReleasesInput(BaseModel):
    """Input schema for list_helm_releases tool."""

    namespace: Optional[str] = Field(
        default="default", description="Namespace to list releases in"
    )
    all_namespaces: Optional[bool] = Field(
        default=False, description="List releases across all namespaces (only when explicitly requested)"
    )
    status_filter: Optional[str] = Field(
        default=None,
        description="Only releases in this state: failed, pending, deployed, superseded, uninstalling",
    )


class GetHelmReleaseInput(BaseModel):
    """Input schema for get_helm_release tool."""

    release: str = Field(description="Helm release name")
    namespace: str = Field(description="Namespace of the release")
    sections: Optional[List[str]] = Field(
        default=None,
        description="Which sections to fetch: status, history, values, manifest, "
        "hooks, notes, metadata. Defaults to status/history/values; request the "
        "others only when needed (each runs an extra helm call). 'hooks' surfaces "
        "failed release hooks; 'manifest'/'hooks' may be large.",
    )
    revision: Optional[int] = Field(
        default=None,
        description="Read a specific past revision (positive integer) for "
        "status/values/manifest/hooks/notes — use two calls to diff revisions.",
    )


class InvestigateHelmReleaseInput(BaseModel):
    """Input schema for investigate_helm_release tool."""

    release: str = Field(description="Helm release name")
    namespace: str = Field(description="Namespace of the release")


class DiffHelmRevisionsInput(BaseModel):
    """Input schema for diff_helm_revisions tool."""

    release: str = Field(description="Helm release name")
    namespace: str = Field(description="Namespace of the release")
    from_revision: int = Field(description="Base revision (positive integer)")
    to_revision: int = Field(description="Target revision (positive integer)")
    section: Optional[str] = Field(
        default="values",
        description="What to diff: 'values' (default) or 'manifest'",
    )


class ListServicesInput(BaseModel):
    """Input schema for list_services tool."""

    namespace: Optional[str] = Field(
        default="default",
        description="Namespace to list services from"
    )


class DescribePodInput(BaseModel):
    """Input schema for describe_pod tool."""
    
    namespace: str = Field(
        description="Namespace containing the pod"
    )
    pod_name: str = Field(
        description="Name of the pod to describe"
    )


class GetPodLogsInput(BaseModel):
    """Input schema for get_pod_logs tool."""
    
    namespace: Optional[str] = Field(
        default=None,
        description="Namespace containing the pod"
    )
    pod_name: str = Field(
        description="Name of the pod"
    )
    previous: bool = Field(
        default=False,
        description="Get logs from previous container instance (useful for CrashLoopBackOff)"
    )
    tail: int = Field(
        default=200,
        description="Number of log lines to retrieve (will be capped by server settings)"
    )
    container: Optional[str] = Field(
        default=None,
        description="Container name for multi-container pods"
    )


class GetEventsInput(BaseModel):
    """Input schema for get_events tool."""
    
    namespace: Optional[str] = Field(
        default="default",
        description="Namespace to query for events"
    )
    field_selector: Optional[str] = Field(
        default=None,
        description="Optional field selector for filtering events"
    )


class GetDeploymentInput(BaseModel):
    """Input schema for get_deployment tool."""
    
    namespace: Optional[str] = Field(
        default=None,
        description="Namespace containing the deployment"
    )
    deployment_name: str = Field(
        description="Name of the deployment"
    )
    labels_only: bool = Field(
        default=False,
        description="Return only deployment labels, selector, and pod template labels"
    )
    images_only: bool = Field(
        default=False,
        description="Return only pod template container images"
    )
    resources_only: bool = Field(
        default=False,
        description="Return only pod template container resource requests/limits"
    )
    template_only: bool = Field(
        default=False,
        description="Return only selector and safe pod template summary"
    )


class GetServiceInput(BaseModel):
    """Input schema for get_service tool."""
    
    namespace: Optional[str] = Field(
        default=None,
        description="Namespace containing the service"
    )
    service_name: str = Field(
        description="Name of the service"
    )
    ports_only: bool = Field(
        default=False,
        description="Return only service type and structured port mappings"
    )
    selector_only: bool = Field(
        default=False,
        description="Return only service labels and selector"
    )
    traffic_policy_only: bool = Field(
        default=False,
        description="Return only traffic policy, IP family, session affinity, and load balancer fields"
    )


class GetEndpointsInput(BaseModel):
    """Input schema for get_endpoints tool."""
    
    namespace: Optional[str] = Field(
        default=None,
        description="Namespace containing the service"
    )
    service_name: str = Field(
        description="Name of the service to check endpoints for"
    )
    include_slices: bool = Field(
        default=True,
        description="Include EndpointSlice-backed readiness, targetRef, node, zone, and port details"
    )


class GetRolloutStatusInput(BaseModel):
    """Input schema for get_rollout_status tool."""
    
    namespace: Optional[str] = Field(
        default=None,
        description="Namespace containing the deployment"
    )
    deployment_name: str = Field(
        description="Name of the deployment"
    )


class K8sgptAnalyzeInput(BaseModel):
    """Input schema for k8sgpt_analyze tool."""
    
    namespace: Optional[str] = Field(
        default=None,
        description="Optional namespace to analyze (analyzes all if not specified)"
    )
    filter_text: Optional[str] = Field(
        default=None,
        description="Optional filter for k8sgpt analysis"
    )


class AddKubeconfigContextInput(BaseModel):
    """Input schema for add_kubeconfig_context tool."""
    
    ssh_connection: str = Field(
        description="SSH connection string (e.g., 'user@hostname' or 'ansible@sabs12-gfs-k8s-m01.example.com')"
    )
    password: Optional[str] = Field(
        default=None,
        description="Optional SSH password (if not using key-based auth). WARNING: Use with caution."
    )
    context_name: Optional[str] = Field(
        default=None,
        description="Optional custom name for the context (defaults to hostname)"
    )
    port: int = Field(
        default=22,
        description="SSH port (default: 22)"
    )


class ListKubeconfigContextsInput(BaseModel):
    """Input schema for list_kubeconfig_contexts tool."""
    pass


class SwitchKubeconfigContextInput(BaseModel):
    """Input schema for switch_kubeconfig_context tool."""
    
    context_name: str = Field(
        description="Name of the context to switch to"
    )


class GetCurrentContextInput(BaseModel):
    """Input schema for get_current_context tool."""
    pass


class SearchDeploymentRepoInput(BaseModel):
    """Input schema for search_deployment_repo tool."""
    
    query: str = Field(
        description="Search query (e.g., 'ansible playbook', 'helm chart', 'deployment config')"
    )
    path_filter: Optional[str] = Field(
        default=None,
        description="Optional path filter (e.g., 'ansible/', 'helm/', 'infra/')"
    )
    file_extension: Optional[str] = Field(
        default=None,
        description="Optional file extension filter (e.g., '.yaml', '.yml', '.sh')"
    )


class GetDeploymentRepoFileInput(BaseModel):
    """Input schema for get_deployment_repo_file tool."""
    
    file_path: str = Field(
        description="Relative path to file in deployment-provisioning repo"
    )


class ListDeploymentRepoPathInput(BaseModel):
    """Input schema for list_deployment_repo_path tool."""
    
    path: str = Field(
        default="",
        description="Relative path in deployment-provisioning repo (default: root)"
    )


class InvestigatePodInput(BaseModel):
    """Input schema for investigate_pod tool."""

    namespace: Optional[str] = Field(
        default=None,
        description="Namespace containing the pod"
    )
    pod_name: str = Field(
        description="Name of the pod to investigate end-to-end"
    )
    tail: int = Field(
        default=200,
        description="Log tail lines for log steps (capped by server settings)"
    )
    use_ai: bool = Field(
        default=True,
        description="Run LLM analysis on the collected kubectl data using the configured provider"
    )


class ExecPodCommandInput(BaseModel):
    """Input schema for exec_pod_command tool."""

    namespace: str = Field(
        description="Namespace containing the pod"
    )
    pod_name: str = Field(
        description="Name of the pod to execute command in"
    )
    command: str = Field(
        description="Command to execute (e.g., 'ls -lh /var/lib/postgresql/pg_wal/')"
    )
    container: Optional[str] = Field(
        default=None,
        description="Container name for multi-container pods"
    )
    confirm: bool = Field(
        default=False,
        description="REQUIRED: Must be set to true to confirm execution. This is a write operation."
    )


class DeletePodInput(BaseModel):
    """Input schema for delete_pod tool."""

    namespace: str = Field(
        description="Namespace containing the pod"
    )
    pod_name: str = Field(
        description="Name of the pod to delete"
    )
    grace_period: int = Field(
        default=30,
        description="Grace period in seconds for pod termination (default: 30)"
    )
    confirm: bool = Field(
        default=False,
        description="REQUIRED: Must be set to true to confirm deletion. This is a destructive operation."
    )
    dry_run: bool = Field(
        default=False,
        description="When true, runs kubectl --dry-run=server and returns a preview plus a single-use confirmation_token."
    )
    confirmation_token: Optional[str] = Field(
        default=None,
        description="Single-use token returned by a prior dry_run=True call. Required when strict confirmation is enabled."
    )


class RolloutRestartInput(BaseModel):
    """Input schema for rollout_restart tool."""

    namespace: str = Field(
        description="Namespace containing the deployment"
    )
    deployment_name: str = Field(
        description="Name of the deployment to restart"
    )
    confirm: bool = Field(
        default=False,
        description="REQUIRED: Must be set to true to confirm restart. This will restart all pods."
    )
    dry_run: bool = Field(
        default=False,
        description="When true, runs kubectl --dry-run=server and returns a preview plus a single-use confirmation_token."
    )
    confirmation_token: Optional[str] = Field(
        default=None,
        description="Single-use token returned by a prior dry_run=True call. Required when strict confirmation is enabled."
    )


class ScaleDeploymentInput(BaseModel):
    """Input schema for scale_deployment tool."""

    namespace: str = Field(
        description="Namespace containing the deployment"
    )
    deployment_name: str = Field(
        description="Name of the deployment to scale"
    )
    replicas: int = Field(
        description="Target number of replicas",
        ge=0
    )
    confirm: bool = Field(
        default=False,
        description="REQUIRED: Must be set to true to confirm scaling. This will change replica count."
    )
    dry_run: bool = Field(
        default=False,
        description="When true, runs kubectl --dry-run=server and returns a preview plus a single-use confirmation_token."
    )
    confirmation_token: Optional[str] = Field(
        default=None,
        description="Single-use token returned by a prior dry_run=True call. Required when strict confirmation is enabled."
    )


class ApplyPatchInput(BaseModel):
    """Input schema for apply_patch tool."""

    namespace: str = Field(
        description="Namespace containing the resource"
    )
    resource_type: str = Field(
        description="Resource type (e.g., 'deployment', 'statefulset', 'pod')"
    )
    resource_name: str = Field(
        description="Name of the resource to patch"
    )
    patch: str = Field(
        description="JSON patch to apply (e.g., '{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"app\",\"resources\":{\"limits\":{\"memory\":\"1Gi\"}}}]}}}}')"
    )
    patch_type: str = Field(
        default="strategic",
        description="Patch type: 'strategic', 'merge', or 'json' (default: strategic)"
    )
    confirm: bool = Field(
        default=False,
        description="REQUIRED: Must be set to true to confirm patch. This will modify the resource."
    )
    dry_run: bool = Field(
        default=False,
        description="When true, runs kubectl --dry-run=server and returns a preview plus a single-use confirmation_token."
    )
    confirmation_token: Optional[str] = Field(
        default=None,
        description="Single-use token returned by a prior dry_run=True call. Required when strict confirmation is enabled."
    )


class GetResourceGraphInput(BaseModel):
    """Input schema for get_resource_graph tool."""

    namespace: Optional[str] = Field(
        default="default",
        description="Namespace to generate the resource graph for"
    )

class InvestigateWorkloadInput(BaseModel):
    """Input schema for investigate_workload tool."""
    namespace: Optional[str] = Field(default=None, description="Namespace containing the workload")
    workload_name: str = Field(description="Name of the workload")
    workload_type: str = Field(default="deployment", description="Type of workload (deployment, statefulset, daemonset)")
    use_ai: bool = Field(default=True, description="Run LLM analysis using the configured provider")

class AnalyzeNamespaceInput(BaseModel):
    """Input schema for analyze_namespace tool."""
    namespace: str = Field(description="Namespace to analyze for holistic health")


# ── AI Analysis Tool Schemas ──────────────────────────────────────────────────

class AnalyzeErrorInput(BaseModel):
    """Input schema for analyze_error tool."""

    error_text: str = Field(
        description="Raw error text to analyze (paste from logs, terminal, CI/CD pipeline, etc.)"
    )
    tool: str = Field(
        default="kubernetes",
        description="Tool type: 'kubernetes', 'ansible', or 'helm'"
    )
    environment: str = Field(
        default="production",
        description="Environment context (e.g., 'production', 'staging', 'dev')"
    )
    structured_payload: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional structured Ansible/kubernetes.core.k8s result. Status "
            "conditions and container images are treated as caller-supplied "
            "evidence, not live-cluster observations."
        ),
    )
    diagnostic_mode: Optional[Literal["error_only"]] = Field(
        default=None,
        description=(
            "Set to error_only when no live-cluster tools are available. "
            "Enforces an explicit evidence boundary in the analysis prompt."
        ),
    )


class GetFixCommandsInput(BaseModel):
    """Input schema for get_fix_commands tool."""

    error_text: Optional[str] = Field(
        default=None,
        description="Raw error text (will auto-classify). Provide this OR category."
    )
    category: Optional[str] = Field(
        default=None,
        description="Known error category (e.g., 'pod_crashloop', 'pod_oom', 'rbac'). Provide this OR error_text."
    )
    tool: str = Field(
        default="kubernetes",
        description="Tool type: 'kubernetes' or 'ansible'"
    )
    namespace: str = Field(
        default="<namespace>",
        description="Kubernetes namespace to substitute into commands"
    )
    resource_name: str = Field(
        default="<name>",
        description="Resource name (pod/deployment) to substitute into commands"
    )


class ListErrorCategoriesInput(BaseModel):
    """Input schema for list_error_categories tool (no parameters required)."""
    pass


class ClusterReportInput(BaseModel):
    """Input schema for cluster_report tool."""

    events_text: str = Field(
        description="Paste the output of: kubectl get events --all-namespaces --sort-by='.lastTimestamp'"
    )
    namespace: str = Field(
        default="all",
        description="Namespace context for the report (informational)"
    )


class ErrorSummaryInput(BaseModel):
    """Input schema for error_summary tool."""

    errors: List[str] = Field(
        description="List of error strings to summarize (e.g., from a CI/CD pipeline run)"
    )
    tool: str = Field(
        default="kubernetes",
        description="Tool type: 'kubernetes' or 'ansible'"
    )


class GenerateRunbookInput(BaseModel):
    """Input schema for generate_runbook tool."""

    category: Optional[str] = Field(
        default=None,
        description="Error category name (e.g., 'pod_crashloop'). Provide this OR error_text."
    )
    error_text: Optional[str] = Field(
        default=None,
        description="Raw error text (will auto-classify to a category)"
    )
    error_examples: Optional[List[str]] = Field(
        default=None,
        description="Optional list of example error strings for richer runbook context"
    )
    tool: str = Field(
        default="kubernetes",
        description="Tool type: 'kubernetes' or 'ansible'"
    )


# ── Multi-step remediation plans (Feature C) ────────────────────────────────

class ProposeRemediationPlanInput(BaseModel):
    """Input schema for propose_remediation_plan."""

    issue: str = Field(
        description="One-sentence description of the problem the plan addresses."
    )
    steps: List[Dict[str, Any]] = Field(
        description=(
            "Ordered list of step dicts. Each step: "
            '{"tool": "<one of delete_pod|rollout_restart|scale_deployment|apply_patch>", '
            '"args": {...}, "why": "...", "rollback": {...} (optional)}'
        )
    )
    notes: Optional[str] = Field(
        default=None,
        description="Optional free-form notes (e.g., risk callouts)."
    )


class GetPlanInput(BaseModel):
    """Input schema for get_plan."""

    plan_id: str = Field(
        description="Plan id returned by propose_remediation_plan."
    )


class ExecutePlanStepInput(BaseModel):
    """Input schema for execute_plan_step."""

    plan_id: str = Field(
        description="Plan id returned by propose_remediation_plan."
    )
    step_index: int = Field(
        description="Zero-based index of the step to execute.",
        ge=0,
    )
    confirmation_token: str = Field(
        description=(
            "Single-use token from a prior dry_run=True call on the same "
            "destructive tool with the same args. Required (Feature B)."
        )
    )


# ── RAG knowledge-base search (Phase 1.2) ────────────────────────────────────

class KbSearchInput(BaseModel):
    """Input schema for kb_search."""

    query: str = Field(
        description=(
            "Natural-language query. Embedded with the same model used by "
            "the ingestion pipeline so semantically close chunks score high."
        )
    )
    collection: Optional[str] = Field(
        default=None,
        description=(
            "Which knowledge base to search. Defaults to devops_doc "
            "(ingested team docs). Valid: devops_doc | runbook | "
            "session_memory | k8s_errors."
        ),
    )
    namespace: Optional[str] = Field(
        default=None,
        description="Optional equality filter on payload.namespace.",
    )
    cluster: Optional[str] = Field(
        default=None,
        description="Optional equality filter on payload.cluster.",
    )
    kind: Optional[str] = Field(
        default=None,
        description="Optional equality filter on payload.kind (k8s resource kind).",
    )
    verified_only: bool = Field(
        default=False,
        description="When true, only return entries marked verified=true.",
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Max number of hits to return.",
    )


class PromQueryInput(BaseModel):
    """Input schema for prom_query tool (Prometheus instant query)."""
    query: str = Field(..., description="PromQL expression to evaluate.")
