"""MCP tool registrations for the unified KubeAstra MCP server.

Registers 37 tools split across three categories:
  • 27 live kubectl tools  — real-time cluster investigation & recovery
  •  6 AI analysis tools  — LLM-powered error analysis, fix playbooks, runbooks
  •  3 plan tools          — multi-step remediation planning (Feature C)

Live tools (kubectl-based):
  find_workload, get_pods, get_namespaces, get_nodes, investigate_node, list_namespace_resources,
  list_services, describe_pod, get_pod_logs, get_events, get_deployment,
  get_service, get_endpoints, get_rollout_status, k8sgpt_analyze,
  add_kubeconfig_context, list_kubeconfig_contexts, switch_kubeconfig_context,
  get_current_context, search_deployment_repo, get_deployment_repo_file,
  list_deployment_repo_path, investigate_pod (+ AI analysis),
  exec_pod_command, delete_pod, rollout_restart, scale_deployment, apply_patch

AI tools (Gemini + RAG):
  analyze_error, get_fix_commands, list_error_categories,
  cluster_report, error_summary, generate_runbook

Plan tools (Feature C):
  propose_remediation_plan, get_plan, execute_plan_step
"""

import logging
from typing import Any, Dict

from mcp.server import Server
from mcp.types import Tool, TextContent

from k8s.wrappers import (
    find_workload, get_pods, get_namespaces, get_nodes, investigate_node, list_namespace_resources, list_services,
    get_configmap, search_configmaps,
    describe_pod, get_pod_logs, get_events,
    get_deployment, get_service, get_endpoints, get_rollout_status,
    k8sgpt_analyze, add_kubeconfig_context, list_kubeconfig_contexts,
    switch_kubeconfig_context, get_current_context, search_deployment_repo,
    get_deployment_repo_file, list_deployment_repo_path, investigate_pod,
    exec_pod_command, delete_pod, rollout_restart, scale_deployment, apply_patch,
    get_resource_graph, investigate_workload, analyze_namespace,
)
from k8s.helm_wrappers import (
    helm_available, list_helm_releases, get_helm_release, diff_helm_revisions,
    investigate_helm_release,
)
from k8s.validators import ValidationError
from k8s.kubectl_runner import KubectlError

import ai_tools.analyze as _analyze_tool
import ai_tools.fix as _fix_tool
import ai_tools.report as _report_tool
import ai_tools.runbook as _runbook_tool

from mcp_server.schemas import (
    # Live kubectl schemas
    FindWorkloadInput, GetPodsInput, GetNamespacesInput, GetNodesInput, InvestigateNodeInput, ListNamespaceResourcesInput,
    GetConfigMapInput, SearchConfigMapsInput,
    HelmAvailableInput, ListHelmReleasesInput, GetHelmReleaseInput, DiffHelmRevisionsInput,
    InvestigateHelmReleaseInput,
    ListServicesInput, DescribePodInput, GetPodLogsInput,
    GetEventsInput, GetDeploymentInput, GetServiceInput, GetEndpointsInput,
    GetRolloutStatusInput, K8sgptAnalyzeInput, AddKubeconfigContextInput,
    ListKubeconfigContextsInput, SwitchKubeconfigContextInput, GetCurrentContextInput,
    SearchDeploymentRepoInput, GetDeploymentRepoFileInput, ListDeploymentRepoPathInput,
    InvestigatePodInput, ExecPodCommandInput, DeletePodInput, RolloutRestartInput,
    ScaleDeploymentInput, ApplyPatchInput, GetResourceGraphInput,
    InvestigateWorkloadInput, AnalyzeNamespaceInput,
    # AI tool schemas
    AnalyzeErrorInput, GetFixCommandsInput, ListErrorCategoriesInput,
    ClusterReportInput, ErrorSummaryInput, GenerateRunbookInput,
    # Plan tool schemas (Feature C)
    ProposeRemediationPlanInput, GetPlanInput, ExecutePlanStepInput,
    # RAG search (Phase 1.2)
    KbSearchInput,
)

logger = logging.getLogger(__name__)


def get_tools_definitions() -> list[Tool]:
    """Return the complete list of tool definitions. Extracted for reuse in runtime and server initialization."""
    return [
            # ── Live Kubectl Tools ──────────────────────────────────────────────
            Tool(
                name="find_workload",
                description=(
                    "Search for matching workloads (deployments, pods, services) across allowed namespaces. "
                    "Use when you know the service/workload name but not the namespace. "
                    "Optionally provide an environment hint (prod, staging, dev) to prioritize search."
                ),
                inputSchema=FindWorkloadInput.model_json_schema()
            ),
            Tool(
                name="get_pods",
                description=(
                    "List pods in a namespace with optional label selector or status filter. "
                    "Use as a first step to understand pod health and identify unhealthy pods. "
                    "Returns pod summaries including phase, ready status, restart count, and node placement. "
                    "For focused inventory questions, set labels_only, images_only, resources_only, "
                    "or placement_only=true to return only the requested pod fields."
                ),
                inputSchema=GetPodsInput.model_json_schema()
            ),
            Tool(
                name="get_namespaces",
                description=(
                    "List all namespaces in the current cluster with status and labels. "
                    "Use this when you need to discover the available namespaces before drilling deeper."
                ),
                inputSchema=GetNamespacesInput.model_json_schema()
            ),
            Tool(
                name="get_nodes",
                description=(
                    "List all Kubernetes nodes in the current cluster with readiness, kubelet version, "
                    "OS image, labels, taints, addresses, full conditions, capacity, and allocatable CPU/memory. "
                    "Use when a user asks how many nodes exist, whether nodes are ready, or wants node labels, "
                    "taints, conditions, or addresses. For focused questions, set labels_only, taints_only, "
                    "conditions_only, or addresses_only=true."
                ),
                inputSchema=GetNodesInput.model_json_schema()
            ),
            Tool(
                name="investigate_node",
                description=(
                    "Inspect one Kubernetes node's capacity, allocatable CPU/memory, current pod CPU "
                    "requests/limits, and readiness conditions. Use when a user asks about allocated "
                    "CPU/resources for a specific node."
                ),
                inputSchema=InvestigateNodeInput.model_json_schema()
            ),
            Tool(
                name="list_namespace_resources",
                description=(
                    "Get an aggregate view of everything running in a namespace. "
                    "Returns pods, services, deployments, statefulsets, daemonsets, "
                    "configmaps, PVCs, and ingresses with safe labels, selectors, "
                    "workload images, replica health, ingress backend paths, and PVC capacity."
                ),
                inputSchema=ListNamespaceResourcesInput.model_json_schema()
            ),
            Tool(
                name="search_configmaps",
                description=(
                    "Find which ConfigMap in a namespace contains a value or key (read-only). "
                    "Use when you know a failing value (e.g. a pinned version) but not where "
                    "it is defined. Returns the owning ConfigMap, key, and a redacted excerpt."
                ),
                inputSchema=SearchConfigMapsInput.model_json_schema()
            ),
            Tool(
                name="get_configmap",
                description=(
                    "Read a single ConfigMap's data by name (read-only). With a key, returns "
                    "that key's redacted, size-capped value; without a key, returns keys, "
                    "labels/annotations, and small previews. Secret values are never returned."
                ),
                inputSchema=GetConfigMapInput.model_json_schema()
            ),
            Tool(
                name="helm_available",
                description=(
                    "Check whether Helm is installed and reachable on the active target. "
                    "Returns availability and version. Read-only."
                ),
                inputSchema=HelmAvailableInput.model_json_schema()
            ),
            Tool(
                name="list_helm_releases",
                description=(
                    "List Helm releases in a namespace (or all namespaces only when "
                    "explicitly requested), optional status_filter "
                    "(failed/pending/deployed/superseded/uninstalling): name, namespace, "
                    "revision, status, chart, app version, updated time. Read-only."
                ),
                inputSchema=ListHelmReleasesInput.model_json_schema()
            ),
            Tool(
                name="get_helm_release",
                description=(
                    "Read a named Helm release: status/history/values by default; "
                    "manifest/hooks/notes/metadata on request; revision=N for a past "
                    "revision (diff via two calls). Values/manifests/hooks/notes are "
                    "redacted and capped. Read-only."
                ),
                inputSchema=GetHelmReleaseInput.model_json_schema()
            ),
            Tool(
                name="diff_helm_revisions",
                description=(
                    "Unified diff of two revisions of a Helm release's values "
                    "(default) or rendered manifest — 'what changed in the last "
                    "upgrade?'. Redacted before diffing (no secret leakage), so "
                    "changed=false means 'no non-secret changes' (a secret-only "
                    "change is hidden, flagged by "
                    "redaction_may_hide_secret_only_changes). Capped. Read-only."
                ),
                inputSchema=DiffHelmRevisionsInput.model_json_schema()
            ),
            Tool(
                name="investigate_helm_release",
                description=(
                    "Composite read-only Helm release investigation: status, recent "
                    "revisions, rendered resources, live pod health and warning events, "
                    "with a health assessment. Manifests redacted before parsing. Read-only."
                ),
                inputSchema=InvestigateHelmReleaseInput.model_json_schema()
            ),
            Tool(
                name="list_services",
                description=(
                    "List all services in a namespace with type, cluster IP, ports, and selectors. "
                    "Use when the user wants all services rather than details for a single one."
                ),
                inputSchema=ListServicesInput.model_json_schema()
            ),
            Tool(
                name="describe_pod",
                description=(
                    "Get detailed pod description with parsed highlights. "
                    "Use after identifying a failing pod to understand its state, conditions, and recent events. "
                    "Returns restart count, current state, last state, and readiness information."
                ),
                inputSchema=DescribePodInput.model_json_schema()
            ),
            Tool(
                name="get_pod_logs",
                description=(
                    "Get pod logs with size limits. "
                    "Use previous=True for CrashLoopBackOff investigations to see logs from the crashed container. "
                    "Logs are automatically truncated to prevent memory issues."
                ),
                inputSchema=GetPodLogsInput.model_json_schema()
            ),
            Tool(
                name="get_events",
                description=(
                    "Get recent events in a namespace sorted by timestamp. "
                    "Use for scheduling issues, image pull errors, probe failures, and other cluster events."
                ),
                inputSchema=GetEventsInput.model_json_schema()
            ),
            Tool(
                name="get_deployment",
                description=(
                    "Get deployment status and details including replica counts and conditions. "
                    "Use to understand deployment health, rollout status, and scaling issues. "
                    "For focused questions, set labels_only, images_only, resources_only, "
                    "or template_only=true to return only the requested deployment fields."
                ),
                inputSchema=GetDeploymentInput.model_json_schema()
            ),
            Tool(
                name="get_service",
                description=(
                    "Get service details including labels, selector, structured ports, type, "
                    "load balancer status, traffic policies, IP families, and session affinity. "
                    "Use ports_only, selector_only, or traffic_policy_only for focused questions."
                ),
                inputSchema=GetServiceInput.model_json_schema()
            ),
            Tool(
                name="get_endpoints",
                description=(
                    "Get service endpoints to check if pods are backing the service. "
                    "Use when a service has no endpoints or when investigating connectivity issues. "
                    "Includes EndpointSlice readiness when available: ready, serving, "
                    "terminating, targetRef, nodeName, zone/topology hints, and ports."
                ),
                inputSchema=GetEndpointsInput.model_json_schema()
            ),
            Tool(
                name="get_rollout_status",
                description=(
                    "Get rollout status for a deployment. "
                    "Use when investigating stuck rollouts or deployment updates."
                ),
                inputSchema=GetRolloutStatusInput.model_json_schema()
            ),
            Tool(
                name="k8sgpt_analyze",
                description=(
                    "Run k8sgpt analysis for broader cluster insights (requires k8sgpt CLI). "
                    "Use only as a supporting step when targeted checks are insufficient."
                ),
                inputSchema=K8sgptAnalyzeInput.model_json_schema()
            ),
            Tool(
                name="add_kubeconfig_context",
                description=(
                    "Add a new Kubernetes cluster context via SSH. "
                    "Use to dynamically add cluster contexts without restarting the server. "
                    "Supports key-based and password-based SSH auth. "
                    "Example: ssh_connection='ansible@hostname.example.com'"
                ),
                inputSchema=AddKubeconfigContextInput.model_json_schema()
            ),
            Tool(
                name="list_kubeconfig_contexts",
                description=(
                    "List all available kubeconfig contexts and show which one is currently active."
                ),
                inputSchema=ListKubeconfigContextsInput.model_json_schema()
            ),
            Tool(
                name="switch_kubeconfig_context",
                description=(
                    "Switch to a different kubeconfig context. "
                    "All subsequent kubectl commands will target the selected cluster."
                ),
                inputSchema=SwitchKubeconfigContextInput.model_json_schema()
            ),
            Tool(
                name="get_current_context",
                description="Get the current active kubeconfig context (which cluster is active).",
                inputSchema=GetCurrentContextInput.model_json_schema()
            ),
            Tool(
                name="search_deployment_repo",
                description=(
                    "Search the deployment-provisioning repository for Ansible playbooks, Helm charts, "
                    "and infrastructure configurations. Supports content search and filename matching."
                ),
                inputSchema=SearchDeploymentRepoInput.model_json_schema()
            ),
            Tool(
                name="get_deployment_repo_file",
                description=(
                    "Get the full contents of a specific file from the deployment-provisioning repository. "
                    "Use after finding relevant files with search_deployment_repo."
                ),
                inputSchema=GetDeploymentRepoFileInput.model_json_schema()
            ),
            Tool(
                name="list_deployment_repo_path",
                description=(
                    "List files and directories in the deployment-provisioning repository. "
                    "Use to explore Ansible playbooks, Helm charts, and infrastructure configurations."
                ),
                inputSchema=ListDeploymentRepoPathInput.model_json_schema()
            ),
            Tool(
                name="get_resource_graph",
                description=(
                    "Get an interactive visual resource graph mapping the relationships between "
                    "Ingresses, Services, Deployments, and Pods in a namespace. "
                    "Use when a user asks to map, visualize, or show a graph of the namespace or workloads."
                ),
                inputSchema=GetResourceGraphInput.model_json_schema()
            ),
            Tool(
                name="investigate_workload",
                description=(
                    "Investigate a specific workload (Deployment, StatefulSet, DaemonSet) "
                    "by gathering its definition, associated pods, and events. "
                    "Optionally runs an LLM root-cause analysis using the configured provider."
                ),
                inputSchema=InvestigateWorkloadInput.model_json_schema()
            ),
            Tool(
                name="analyze_namespace",
                description=(
                    "Holistic health check for a namespace, combining resource overview "
                    "with Warning events and AI analysis for systemic/cascading failures."
                ),
                inputSchema=AnalyzeNamespaceInput.model_json_schema()
            ),
            Tool(
                name="investigate_pod",
                description=(
                    "End-to-end investigation for a pod using failure-mode playbooks + optional AI diagnosis. "
                    "Automatically classifies Pending, ImagePullBackOff, or CrashLoopBackOff and runs "
                    "the right kubectl tool chain. If use_ai=True (default) and the configured LLM provider is available, "
                    "appends an AI root-cause analysis and fix commands to the kubectl findings. "
                    "Use this as your primary triage tool."
                ),
                inputSchema=InvestigatePodInput.model_json_schema()
            ),
            Tool(
                name="exec_pod_command",
                description=(
                    "Execute a command inside a pod container. WRITE OPERATION requiring user approval. "
                    "Requires confirm=True to execute. Returns command output."
                ),
                inputSchema=ExecPodCommandInput.model_json_schema()
            ),
            Tool(
                name="delete_pod",
                description=(
                    "Delete a pod to force restart. DESTRUCTIVE OPERATION requiring user approval. "
                    "If managed by a controller the pod will be recreated. Requires confirm=True."
                ),
                inputSchema=DeletePodInput.model_json_schema()
            ),
            Tool(
                name="rollout_restart",
                description=(
                    "Perform a rolling restart of a deployment. WRITE OPERATION requiring user approval. "
                    "Triggers a rolling update that recreates pods one by one. Requires confirm=True."
                ),
                inputSchema=RolloutRestartInput.model_json_schema()
            ),
            Tool(
                name="scale_deployment",
                description=(
                    "Scale a deployment to a specific number of replicas. WRITE OPERATION requiring user approval. "
                    "Scale to 0 to stop all pods. Requires confirm=True."
                ),
                inputSchema=ScaleDeploymentInput.model_json_schema()
            ),
            Tool(
                name="apply_patch",
                description=(
                    "Apply a JSON patch to a Kubernetes resource. WRITE OPERATION requiring user approval. "
                    "Use to modify memory limits, env vars, or image tags. Requires confirm=True."
                ),
                inputSchema=ApplyPatchInput.model_json_schema()
            ),

            # ── Multi-step remediation plans (Feature C) ────────────────────────
            Tool(
                name="propose_remediation_plan",
                description=(
                    "Propose a multi-step remediation plan made up of allow-listed destructive tools "
                    "(delete_pod, rollout_restart, scale_deployment, apply_patch). Returns a plan_id. "
                    "Execution is per-step and still requires dry_run + confirmation_token."
                ),
                inputSchema=ProposeRemediationPlanInput.model_json_schema()
            ),
            Tool(
                name="get_plan",
                description="Retrieve a stored remediation plan by its plan_id.",
                inputSchema=GetPlanInput.model_json_schema()
            ),
            Tool(
                name="execute_plan_step",
                description=(
                    "Execute one step of a stored plan. Caller must first call the underlying "
                    "destructive tool with dry_run=True to obtain the confirmation_token for that "
                    "specific step (Feature B). WRITE OPERATION."
                ),
                inputSchema=ExecutePlanStepInput.model_json_schema()
            ),

            # ── Knowledge base search (Phase 1.2) ───────────────────────────────
            Tool(
                name="kb_search",
                description=(
                    "Search the ingested DevOps knowledge base (team docs, runbooks, "
                    "captured chat resolutions, seeded errors) by semantic similarity. "
                    "Returns top-N chunks ranked by cosine similarity, each with title, "
                    "url, section breadcrumb, and the source snippet — suitable for "
                    "citing in answers."
                ),
                inputSchema=KbSearchInput.model_json_schema()
            ),

            # ── AI Analysis Tools ───────────────────────────────────────────────
            Tool(
                name="analyze_error",
                description=(
                    "Analyze a pasted Kubernetes or Ansible error with AI + RAG similarity search. "
                    "No live cluster access needed — paste the error text from any log, terminal, or CI/CD pipeline. "
                    "Returns root cause, fix steps, kubectl commands, and similar past cases."
                ),
                inputSchema=AnalyzeErrorInput.model_json_schema()
            ),
            Tool(
                name="get_fix_commands",
                description=(
                    "Get curated fix commands and playbooks for a specific Kubernetes error category. "
                    "Provide either raw error_text (auto-classifies) or a known category name. "
                    "Returns copy-paste ready kubectl commands with explanations."
                ),
                inputSchema=GetFixCommandsInput.model_json_schema()
            ),
            Tool(
                name="list_error_categories",
                description=(
                    "List all supported Kubernetes error categories with descriptions. "
                    "Use this to discover what categories are available for get_fix_commands or generate_runbook."
                ),
                inputSchema=ListErrorCategoriesInput.model_json_schema()
            ),
            Tool(
                name="cluster_report",
                description=(
                    "Analyze pasted kubectl events output and produce an AI-powered cluster health report. "
                    "Paste the output of: kubectl get events --all-namespaces --sort-by='.lastTimestamp' "
                    "Returns event statistics, top issue categories, and an AI executive summary."
                ),
                inputSchema=ClusterReportInput.model_json_schema()
            ),
            Tool(
                name="error_summary",
                description=(
                    "Summarize a batch of error strings (e.g., from a CI/CD pipeline run or log file). "
                    "Pass a list of error strings and get back category breakdown + AI executive summary. "
                    "Useful for post-incident reports and sprint retrospectives."
                ),
                inputSchema=ErrorSummaryInput.model_json_schema()
            ),
            Tool(
                name="generate_runbook",
                description=(
                    "Generate a markdown runbook for a recurring Kubernetes or Ansible error category. "
                    "Provide a category name or raw error text. Output is ready to paste into Confluence or Notion. "
                    "Includes overview, symptoms, diagnosis steps, fix procedures, prevention, and escalation path."
                ),
                inputSchema=GenerateRunbookInput.model_json_schema()
            ),
        ]


def register_tools(server: Server) -> None:
    """Register all tools with the MCP server."""

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return get_tools_definitions()

    @server.call_tool()
    async def call_tool(name: str, arguments: Any) -> list[TextContent]:
        """Handle tool execution requests from Cursor."""
        try:
            # ── Live Kubectl Tools ────────────────────────────────────────────
            if name == "find_workload":
                inp = FindWorkloadInput(**arguments)
                return [TextContent(type="text", text=_fmt(find_workload(inp.name, inp.environment)))]

            elif name == "get_pods":
                inp = GetPodsInput(**arguments)
                return [TextContent(type="text", text=_fmt(get_pods(
                    inp.namespace or "default",
                    inp.label_selector,
                    inp.status_filter,
                    inp.exclude_namespaces,
                    inp.exclude_namespace_prefixes,
                    labels_only=inp.labels_only,
                    images_only=inp.images_only,
                    resources_only=inp.resources_only,
                    placement_only=inp.placement_only,
                    details=inp.details,
                )))]

            elif name == "get_namespaces":
                return [TextContent(type="text", text=_fmt(get_namespaces()))]

            elif name == "get_nodes":
                inp = GetNodesInput(**arguments)
                return [TextContent(type="text", text=_fmt(get_nodes(
                    inp.node_name,
                    labels_only=inp.labels_only,
                    taints_only=inp.taints_only,
                    conditions_only=inp.conditions_only,
                    addresses_only=inp.addresses_only,
                )))]

            elif name == "investigate_node":
                inp = InvestigateNodeInput(**arguments)
                return [TextContent(type="text", text=_fmt(investigate_node(inp.node_name)))]

            elif name == "list_namespace_resources":
                inp = ListNamespaceResourcesInput(**arguments)
                return [TextContent(type="text", text=_fmt(list_namespace_resources(inp.namespace or "default")))]

            elif name == "search_configmaps":
                inp = SearchConfigMapsInput(**arguments)
                return [TextContent(type="text", text=_fmt(search_configmaps(inp.namespace, inp.query, inp.max_matches)))]

            elif name == "get_configmap":
                inp = GetConfigMapInput(**arguments)
                return [TextContent(type="text", text=_fmt(get_configmap(inp.namespace, inp.name, inp.key)))]

            elif name == "helm_available":
                return [TextContent(type="text", text=_fmt(helm_available()))]

            elif name == "list_helm_releases":
                inp = ListHelmReleasesInput(**arguments)
                return [TextContent(type="text", text=_fmt(list_helm_releases(inp.namespace, bool(inp.all_namespaces), inp.status_filter)))]

            elif name == "get_helm_release":
                inp = GetHelmReleaseInput(**arguments)
                return [TextContent(type="text", text=_fmt(get_helm_release(inp.release, inp.namespace, inp.sections, inp.revision)))]

            elif name == "diff_helm_revisions":
                inp = DiffHelmRevisionsInput(**arguments)
                return [TextContent(type="text", text=_fmt(diff_helm_revisions(inp.release, inp.namespace, inp.from_revision, inp.to_revision, inp.section)))]

            elif name == "investigate_helm_release":
                inp = InvestigateHelmReleaseInput(**arguments)
                return [TextContent(type="text", text=_fmt(investigate_helm_release(inp.release, inp.namespace)))]

            elif name == "list_services":
                inp = ListServicesInput(**arguments)
                return [TextContent(type="text", text=_fmt(list_services(inp.namespace or "default")))]

            elif name == "describe_pod":
                inp = DescribePodInput(**arguments)
                return [TextContent(type="text", text=_fmt(describe_pod(inp.namespace, inp.pod_name)))]

            elif name == "get_pod_logs":
                inp = GetPodLogsInput(**arguments)
                return [TextContent(type="text", text=_fmt(get_pod_logs(
                    inp.namespace or "default", inp.pod_name, inp.previous, inp.tail, inp.container
                )))]

            elif name == "get_events":
                inp = GetEventsInput(**arguments)
                return [TextContent(type="text", text=_fmt(get_events(inp.namespace or "default", inp.field_selector)))]

            elif name == "get_deployment":
                inp = GetDeploymentInput(**arguments)
                return [TextContent(type="text", text=_fmt(get_deployment(
                    inp.namespace or "default",
                    inp.deployment_name,
                    labels_only=inp.labels_only,
                    images_only=inp.images_only,
                    resources_only=inp.resources_only,
                    template_only=inp.template_only,
                )))]

            elif name == "get_service":
                inp = GetServiceInput(**arguments)
                return [TextContent(type="text", text=_fmt(get_service(
                    inp.namespace or "default",
                    inp.service_name,
                    ports_only=inp.ports_only,
                    selector_only=inp.selector_only,
                    traffic_policy_only=inp.traffic_policy_only,
                )))]

            elif name == "get_endpoints":
                inp = GetEndpointsInput(**arguments)
                return [TextContent(type="text", text=_fmt(get_endpoints(
                    inp.namespace or "default",
                    inp.service_name,
                    include_slices=inp.include_slices,
                )))]

            elif name == "get_rollout_status":
                inp = GetRolloutStatusInput(**arguments)
                return [TextContent(type="text", text=_fmt(get_rollout_status(inp.namespace or "default", inp.deployment_name)))]

            elif name == "k8sgpt_analyze":
                inp = K8sgptAnalyzeInput(**arguments)
                return [TextContent(type="text", text=_fmt(k8sgpt_analyze(inp.namespace, inp.filter_text)))]

            elif name == "add_kubeconfig_context":
                inp = AddKubeconfigContextInput(**arguments)
                return [TextContent(type="text", text=_fmt(add_kubeconfig_context(
                    inp.ssh_connection, inp.password, inp.context_name, inp.port
                )))]

            elif name == "list_kubeconfig_contexts":
                return [TextContent(type="text", text=_fmt(list_kubeconfig_contexts()))]

            elif name == "switch_kubeconfig_context":
                inp = SwitchKubeconfigContextInput(**arguments)
                return [TextContent(type="text", text=_fmt(switch_kubeconfig_context(inp.context_name)))]

            elif name == "get_current_context":
                return [TextContent(type="text", text=_fmt(get_current_context()))]

            elif name == "search_deployment_repo":
                inp = SearchDeploymentRepoInput(**arguments)
                return [TextContent(type="text", text=_fmt(search_deployment_repo(
                    inp.query, inp.path_filter, inp.file_extension
                )))]

            elif name == "get_deployment_repo_file":
                inp = GetDeploymentRepoFileInput(**arguments)
                return [TextContent(type="text", text=_fmt(get_deployment_repo_file(inp.file_path)))]

            elif name == "list_deployment_repo_path":
                inp = ListDeploymentRepoPathInput(**arguments)
                return [TextContent(type="text", text=_fmt(list_deployment_repo_path(inp.path)))]

            elif name == "get_resource_graph":
                inp = GetResourceGraphInput(**arguments)
                return [TextContent(type="text", text=_fmt(get_resource_graph(inp.namespace or "default")))]

            elif name == "investigate_workload":
                inp = InvestigateWorkloadInput(**arguments)
                return [TextContent(type="text", text=_fmt(investigate_workload(
                    inp.namespace or "default", inp.workload_name, inp.workload_type, inp.use_ai
                )))]

            elif name == "analyze_namespace":
                inp = AnalyzeNamespaceInput(**arguments)
                return [TextContent(type="text", text=_fmt(analyze_namespace(inp.namespace)))]

            elif name == "investigate_pod":
                inp = InvestigatePodInput(**arguments)
                return [TextContent(type="text", text=_fmt(investigate_pod(
                    inp.namespace or "default", inp.pod_name, inp.tail, inp.use_ai
                )))]

            elif name == "exec_pod_command":
                inp = ExecPodCommandInput(**arguments)
                return [TextContent(type="text", text=_fmt(exec_pod_command(
                    inp.namespace, inp.pod_name, inp.command, inp.container, inp.confirm
                )))]

            elif name == "delete_pod":
                inp = DeletePodInput(**arguments)
                return [TextContent(type="text", text=_fmt(delete_pod(
                    inp.namespace, inp.pod_name, inp.grace_period, inp.confirm,
                    dry_run=inp.dry_run, confirmation_token=inp.confirmation_token,
                )))]

            elif name == "rollout_restart":
                inp = RolloutRestartInput(**arguments)
                return [TextContent(type="text", text=_fmt(rollout_restart(
                    inp.namespace, inp.deployment_name, inp.confirm,
                    dry_run=inp.dry_run, confirmation_token=inp.confirmation_token,
                )))]

            elif name == "scale_deployment":
                inp = ScaleDeploymentInput(**arguments)
                return [TextContent(type="text", text=_fmt(scale_deployment(
                    inp.namespace, inp.deployment_name, inp.replicas, inp.confirm,
                    dry_run=inp.dry_run, confirmation_token=inp.confirmation_token,
                )))]

            elif name == "apply_patch":
                inp = ApplyPatchInput(**arguments)
                return [TextContent(type="text", text=_fmt(apply_patch(
                    inp.namespace, inp.resource_type, inp.resource_name,
                    inp.patch, inp.patch_type, inp.confirm,
                    dry_run=inp.dry_run, confirmation_token=inp.confirmation_token,
                )))]

            # ── Multi-step remediation plans (Feature C) ──────────────────────
            elif name == "propose_remediation_plan":
                inp = ProposeRemediationPlanInput(**arguments)
                from services.plans import build_plan, plan_store, audit_plan_event, PlanValidationError
                try:
                    plan = build_plan(
                        issue=inp.issue, steps=inp.steps, notes=inp.notes or "",
                    )
                except PlanValidationError as e:
                    return [TextContent(type="text", text=_fmt({"success": False, "error": str(e)}))]
                plan_store.put(plan)
                audit_plan_event("proposed", plan.plan_id, user=plan.user)
                return [TextContent(type="text", text=_fmt({"success": True, "plan": plan.to_dict()}))]

            elif name == "get_plan":
                inp = GetPlanInput(**arguments)
                from services.plans import plan_store
                plan = plan_store.get(inp.plan_id)
                if plan is None:
                    return [TextContent(type="text", text=_fmt({"success": False, "error": "plan not found or expired"}))]
                return [TextContent(type="text", text=_fmt({"success": True, "plan": plan.to_dict()}))]

            elif name == "execute_plan_step":
                inp = ExecutePlanStepInput(**arguments)
                from services.plans import execute_step
                return [TextContent(type="text", text=_fmt(execute_step(
                    plan_id=inp.plan_id,
                    step_index=inp.step_index,
                    confirmation_token=inp.confirmation_token,
                )))]

            # ── Knowledge base search (Phase 1.2) ─────────────────────────────
            elif name == "kb_search":
                inp = KbSearchInput(**arguments)
                from services.embeddings import embeddings as _emb
                from services.vector_db import vector_db as _vdb
                from services.rag.schema import (
                    DEVOPS_DOC as _DD, RUNBOOK as _RB,
                    SESSION_MEMORY as _SM, K8S_ERROR as _KE,
                    get_collection as _get_coll,
                )

                coll = (inp.collection or _DD.name).strip()
                if _get_coll(coll) is None:
                    return [TextContent(type="text", text=_fmt({
                        "success": False,
                        "error": f"unknown collection '{coll}'. Valid: "
                                 f"{_DD.name}, {_RB.name}, {_SM.name}, {_KE.name}",
                    }))]

                try:
                    _vdb.connect()  # idempotent — keeps the singleton warm
                except Exception as exc:
                    return [TextContent(type="text", text=_fmt({
                        "success": False, "error": f"vector DB unavailable: {exc}",
                    }))]

                filters = {
                    "namespace": inp.namespace,
                    "cluster":   inp.cluster,
                    "kind":      inp.kind,
                }
                if inp.verified_only:
                    filters["verified"] = True
                # Drop None entries; vector_db.search_in already skips them
                # but keeping the filter dict tight makes logs readable.
                filters = {k: v for k, v in filters.items() if v is not None}

                qvec = _emb.embed(inp.query)
                hits = _vdb.search_in(
                    collection=coll, query_vector=qvec,
                    filters=filters or None, limit=inp.limit,
                )
                return [TextContent(type="text", text=_fmt({
                    "success": True, "collection": coll, "count": len(hits),
                    "filters": filters, "results": hits,
                }))]

            # ── AI Analysis Tools ─────────────────────────────────────────────
            elif name == "analyze_error":
                inp = AnalyzeErrorInput(**arguments)
                return [TextContent(type="text", text=_analyze_tool.run(
                    inp.error_text, inp.tool, inp.environment
                ))]

            elif name == "get_fix_commands":
                inp = GetFixCommandsInput(**arguments)
                return [TextContent(type="text", text=_fix_tool.get_fix_commands(
                    inp.error_text, inp.category, inp.tool, inp.namespace, inp.resource_name
                ))]

            elif name == "list_error_categories":
                return [TextContent(type="text", text=_fix_tool.list_categories())]

            elif name == "cluster_report":
                inp = ClusterReportInput(**arguments)
                return [TextContent(type="text", text=_report_tool.cluster_report(
                    inp.events_text, inp.namespace
                ))]

            elif name == "error_summary":
                inp = ErrorSummaryInput(**arguments)
                return [TextContent(type="text", text=_report_tool.error_summary(
                    inp.errors, inp.tool
                ))]

            elif name == "generate_runbook":
                inp = GenerateRunbookInput(**arguments)
                return [TextContent(type="text", text=_runbook_tool.generate_runbook(
                    inp.category, inp.error_examples, inp.error_text, inp.tool
                ))]

            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

        except ValidationError as e:
            logger.error(f"Validation error in {name}: {e}")
            return [TextContent(type="text", text=f"Validation error: {str(e)}")]

        except KubectlError as e:
            logger.error(f"Kubectl error in {name}: {e}")
            return [TextContent(type="text", text=f"Kubectl error: {str(e)}\nStderr: {e.stderr}")]

        except Exception as e:
            logger.exception(f"Unexpected error in {name}")
            return [TextContent(type="text", text=f"Unexpected error: {str(e)}")]


def _fmt(result: Dict[str, Any]) -> str:
    """Format a dict result as indented JSON."""
    import json
    return json.dumps(result, indent=2, default=str)
