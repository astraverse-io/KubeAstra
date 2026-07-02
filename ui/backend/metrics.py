"""Prometheus metrics definitions for telemetry and dashboard tracking (Phase 2)."""

from prometheus_client import Counter, Gauge, Histogram

# Latency histograms
chat_request_duration_seconds = Histogram(
    "chat_request_duration_seconds",
    "End-to-end latency of chat/admin HTTP requests by route.",
    ["route", "status"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0]
)

chat_requests_total = Counter(
    "chat_requests_total",
    "Total count of chat/admin HTTP requests.",
    ["route", "status"]
)

# ReAct reasoning loop metrics
react_iterations = Histogram(
    "react_iterations",
    "Count of ReAct think-act cycles per run.",
    ["route"],
    buckets=[0, 1, 2, 3, 4, 5, 7, 10, 15, 20]
)

# Tool execution metrics
tool_dispatch_duration_seconds = Histogram(
    "tool_dispatch_duration_seconds",
    "Latency of tool execution by tool name.",
    ["tool", "status"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0]
)

tool_dispatch_total = Counter(
    "tool_dispatch_total",
    "Total count of tool executions.",
    ["tool", "status"]
)

# LLM tokens, costs, and quality fallbacks
llm_tokens_total = Counter(
    "llm_tokens_total",
    "Total count of input, output, or cached input tokens.",
    ["model", "surface", "direction"]
)

llm_cost_usd_total = Counter(
    "llm_cost_usd_total",
    "Total cost of LLM calls in USD.",
    ["model"]
)

critic_fallback_total = Counter(
    "critic_fallback_total",
    "Total count of synthesis critic fail-opens.",
    ["check_name"]
)

# Audit file reliability. These are process-local; the backend metrics endpoint
# reports failures from backend-originated kubectl calls.
audit_write_failures_total = Counter(
    "audit_write_failures_total",
    "Audit entries dropped because the audit file could not be written.",
    ["category"],
)

audit_rotations_total = Counter(
    "audit_rotations_total",
    "Audit file rotations completed after reaching the configured size cap.",
)

# Health and machine-agent API metrics.
health_check_duration_seconds = Histogram(
    "health_check_duration_seconds",
    "Duration of individual detailed health checks.",
    ["check"],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 3.0, 5.0],
)

health_check_failures_total = Counter(
    "health_check_failures_total",
    "Detailed health check failures.",
    ["check"],
)

readiness_failures_total = Counter(
    "readiness_failures_total",
    "Readiness checks that returned not-ready.",
    ["reason"],
)

agent_invoke_requests_total = Counter(
    "agent_invoke_requests_total",
    "Machine-agent invocation requests.",
    ["status"],
)

agent_invoke_duration_seconds = Histogram(
    "agent_invoke_duration_seconds",
    "End-to-end machine-agent invocation latency.",
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60, 90, 120],
)

agent_invoke_queue_wait_seconds = Histogram(
    "agent_invoke_queue_wait_seconds",
    "Time spent waiting for machine-agent execution capacity.",
    buckets=[0.001, 0.01, 0.1, 0.5, 1, 2, 5, 10],
)

agent_invoke_rejections_total = Counter(
    "agent_invoke_rejections_total",
    "Machine-agent requests rejected before execution.",
    ["reason"],
)

agent_invoke_failures_total = Counter(
    "agent_invoke_failures_total",
    "Machine-agent execution failures.",
    ["type"],
)

agent_invoke_active_requests = Gauge(
    "agent_invoke_active_requests",
    "Machine-agent invocations currently holding an execution slot.",
)

agent_remote_connection_attempts_total = Counter(
    "agent_remote_connection_attempts_total",
    "Server-managed remote diagnostic connection attempts.",
    ["target_id", "status", "reason"],
)

agent_remote_connection_duration_seconds = Histogram(
    "agent_remote_connection_duration_seconds",
    "SSH connection and Kubernetes identity-preflight latency.",
    ["target_id"],
    buckets=[0.1, 0.5, 1, 2, 3, 5, 10, 15, 30],
)

agent_remote_diagnostic_mode_total = Counter(
    "agent_remote_diagnostic_mode_total",
    "Remote agent responses by diagnostic mode.",
    ["target_id", "mode"],
)

agent_remote_circuit_state = Gauge(
    "agent_remote_circuit_state",
    "Per-process remote diagnostic circuit state (closed=0, half_open=1, open=2).",
    ["target_id"],
)

agent_remote_circuit_open_total = Counter(
    "agent_remote_circuit_open_total",
    "Remote diagnostic attempts skipped because the per-target circuit is open.",
    ["target_id"],
)
