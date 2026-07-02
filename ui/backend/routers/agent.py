"""Bearer-authenticated, bounded machine invocation of the ReAct agent."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from typing import Literal

import db
from caller_scopes import Principal, ScopeRegistry
from ratelimit import RateLimiter
from routers import chat

logger = logging.getLogger(__name__)


class AgentTargetRequest(BaseModel):
    """Server-resolved diagnostic target.

    The caller names a registered ``target_id``; the backend resolves host,
    port, credentials, and host-key trust. The request cannot override any of
    those fields — that is enforced by ``extra='forbid'``.
    """

    model_config = ConfigDict(extra="forbid")

    connection_type: Literal["ssh"]
    target_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    diagnostic_scope: set[Literal["kubernetes"]] = Field(
        default_factory=lambda: {"kubernetes"},
        min_length=1,
        max_length=1,
    )


class AgentInvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: Any
    instruction: str = Field(
        default="Analyze this input and provide a concrete diagnosis and recommended actions.",
        min_length=1,
        max_length=4000,
    )
    context: dict[str, Any] = Field(default_factory=dict)
    target: AgentTargetRequest | None = None


class AgentInvokeStep(BaseModel):
    action: str
    status: str
    duration_ms: int | None = None
    params: dict[str, Any] | None = None


# Three-state diagnostic mode reported back to the caller. ``error_only`` is the
# default for target-less invocations and for any path where the live
# investigation could not start or finish. ``live_cluster_partial`` is for the
# case where SSH + identity preflight succeeded but one or more evidence calls
# failed; the partial evidence we did gather is still returned.
DiagnosticMode = Literal["error_only", "live_cluster", "live_cluster_partial"]

# Bounded enum for ``ConnectionInfo.reason``. Mirrors the metric-label enum in
# plan §14 but is the subset we expose to API callers. Free-form transport
# exception strings must be mapped to one of these before serialization so we
# never leak hostnames, paths, or stack traces in the response.
ConnectionReason = Literal[
    "connect_timeout",
    "auth_failed",
    "host_key_mismatch",
    "host_key_missing",
    "identity_mismatch",
    "bastion_failed",
    "transport_error",
    "circuit_open",
    "target_disabled",
    "unauthorized",
]


class ConnectionInfo(BaseModel):
    """Structured connection metadata that lets callers distinguish a verified
    live investigation from a generic inference."""

    type: Literal["ssh"] | None = None
    target_id: str | None = None
    verified: bool = False
    reason: ConnectionReason | None = None
    connected_host: str | None = None
    kube_system_uid: str | None = None
    duration_ms: int | None = None


class Evidence(BaseModel):
    """One redacted observation collected during a live investigation."""

    source: Literal["kubernetes"]
    tool: str
    summary: str
    target_id: str | None = None
    observed_at: str | None = None


class AgentInvokeResponse(BaseModel):
    request_id: str
    run_id: str | None = None
    status: str
    diagnostic_mode: DiagnosticMode = "error_only"
    connection: ConnectionInfo = Field(default_factory=ConnectionInfo)
    answer: str
    tool_used: str
    tool_result: Any = None
    steps: list[AgentInvokeStep] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    suggested_actions: list[Any] = Field(default_factory=list)
    timing_ms: int
    error: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _enforce_live_state_consistency(self) -> "AgentInvokeResponse":
        # Plan §6.2: the response must be self-consistent so callers can trust
        # the diagnostic_mode label without inspecting other fields. The rules
        # below close every gap where a caller could otherwise read a "verified
        # live" investigation that wasn't actually verified, or an "error_only"
        # response that nevertheless carries live observations.
        if self.diagnostic_mode in ("live_cluster", "live_cluster_partial"):
            if self.connection.type != "ssh":
                raise ValueError(
                    "diagnostic_mode=live_cluster requires connection.type='ssh'"
                )
            if not self.connection.verified:
                raise ValueError(
                    "diagnostic_mode=live_cluster requires connection.verified=true"
                )
            if not self.connection.target_id:
                raise ValueError(
                    "diagnostic_mode=live_cluster requires connection.target_id"
                )
            if not self.connection.kube_system_uid:
                raise ValueError(
                    "diagnostic_mode=live_cluster requires connection.kube_system_uid"
                )
            if self.connection.reason is not None:
                raise ValueError(
                    "connection.reason must be null when diagnostic_mode is live_*"
                )
            if self.diagnostic_mode == "live_cluster" and not self.evidence:
                raise ValueError(
                    "diagnostic_mode=live_cluster requires at least one evidence entry"
                )
            # Every evidence row must come from the verified target. v1 has
            # exactly one target per response, so an entry with a different
            # target_id is either a bug or a cross-environment leak.
            for i, ev in enumerate(self.evidence):
                if ev.target_id is not None and ev.target_id != self.connection.target_id:
                    raise ValueError(
                        f"evidence[{i}].target_id={ev.target_id!r} does not match "
                        f"connection.target_id={self.connection.target_id!r}"
                    )
        else:
            # error_only: must not carry any signal that implies a live
            # investigation actually completed. ``reason`` may be set with a
            # sanitized category for failure paths, but verified=True,
            # kube_system_uid, and evidence must all be absent.
            if self.connection.verified:
                raise ValueError(
                    "diagnostic_mode=error_only is inconsistent with connection.verified=true"
                )
            if self.connection.kube_system_uid is not None:
                raise ValueError(
                    "diagnostic_mode=error_only must not carry connection.kube_system_uid"
                )
            if self.evidence:
                raise ValueError(
                    "diagnostic_mode=error_only must not carry evidence entries"
                )
        return self


# Target resolution, SSH runner installation, identity preflight, and the
# no-local-fallback path are implemented below. The operator feature flag
# remains the deployment-level kill switch.
_TARGET_RESOLUTION_IMPLEMENTED = True


_SECRET_KEYS = {
    "password", "token", "secret", "authorization", "api_key", "apikey",
    "credential", "private_key", "passphrase",
}
_SECRET_KEY_PARTS = frozenset(
    {"password", "passphrase", "token", "secret", "authorization", "credential"}
)
_UNKNOWN_TARGET_METRIC_LABEL = "__unknown__"
_LIVE_EVIDENCE_CATEGORIES = frozenset(
    {"investigation", "discovery", "pod", "cluster", "helm"}
)
_ERROR_ONLY_LIVE_CLAIM_PATTERNS = (
    (
        "first_person_cluster_search",
        re.compile(
            r"\bI\s+(?:searched|checked|queried|inspected|looked\s+(?:through|across|at))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "namespace_search_result",
        re.compile(r"\bnot\s+found\s+in\s+(?:any|the)\s+namespace", re.IGNORECASE),
    ),
    (
        "kubectl_observation",
        re.compile(r"\bkubectl\s+(?:returned|reported|showed|found)\b", re.IGNORECASE),
    ),
    (
        "pod_event_observation",
        re.compile(r"\bpod\s+events?\s+(?:show|shows|showed|indicate|reported)\b", re.IGNORECASE),
    ),
)

# Allowlist of tool-input keys that may surface in the public response.
# Everything else is dropped so free-form reasoning blobs accidentally stored
# under unusual keys cannot leak. Sourced from tool_registry input schemas;
# extend here when new tool parameters land.
_STEP_PARAM_ALLOWLIST = frozenset({
    # Identifiers
    "namespace", "pod", "pod_name", "deployment", "deployment_name",
    "container", "container_name", "node", "node_name", "service",
    "service_name", "workload", "workload_name", "ingress", "kind", "name",
    "resource", "cluster", "cluster_report", "context", "context_name",
    "collection",
    # Selectors and filtering
    "selector", "label_selector", "field_selector", "filters",
    # Log / output controls
    "tail_lines", "tail", "previous", "follow", "since", "since_time",
    "output", "format", "limit", "count", "details", "events", "events_text",
    # Error analysis inputs
    "error", "error_text", "message", "log", "expected_schema",
    # Tool routing / classification
    "tool", "query", "target", "type", "playbook", "helm", "rag", "react",
    "ai", "discovery", "investigation", "session_scoped",
    # Boolean projection flags from describe-style tools
    "addresses_only", "conditions_only", "images_only", "labels_only",
    "only_addresses", "only_conditions", "only_images", "only_labels",
    "only_placement", "only_ports", "only_resources", "only_selector",
    "only_taints", "only_template", "only_traffic_policy", "placement_only",
    "ports_only", "resources_only", "selector_only", "taints_only",
    "template_only", "traffic_policy_only",
})


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<REDACTED>" if _is_secret_key(key) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        try:
            from services.rag.redaction import redact
            return redact(value)
        except Exception:
            # API redaction must fail closed. chat._redact_log_text has a
            # dependency-free fallback for bearer headers and common
            # key=value secret forms.
            return chat._redact_log_text(value)
    return value


def _is_secret_key(key: Any) -> bool:
    raw = str(key)
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw)
    normalized = re.sub(r"[^a-z0-9]+", "_", snake.lower()).strip("_")
    if normalized in _SECRET_KEYS:
        return True
    return bool(set(normalized.split("_")) & _SECRET_KEY_PARTS)


# Token fingerprint helper lives in caller_scopes so the same prefix length
# (8 chars) is used across metrics, audit, and Principal records. Re-export
# under the legacy private name for any future internal users in this file.
from caller_scopes import token_fingerprint as _token_fingerprint  # noqa: E402


def _safe_step_params(value: Any) -> dict[str, Any] | None:
    """Project tool params through an allowlist before redaction.

    Drops every key not on the allowlist so raw model reasoning stored under
    unusual fields (e.g. ``"plan"``, ``"thought"``) cannot leak through the
    public response.
    """
    if not isinstance(value, dict):
        return None
    projected = {k: v for k, v in value.items() if k in _STEP_PARAM_ALLOWLIST}
    return _redact(projected) if projected else None


def _parse_bearer(
    request: Request,
    primary: str,
    previous: str | None,
    scope_registry: "ScopeRegistry | None" = None,
) -> Principal:
    """Authenticate the request and resolve the caller's principal.

    Returns a ``Principal`` (sanitized — no token material). The legacy
    ``primary`` / ``previous`` tokens are matched in addition to the scoped
    registry so existing target-less callers keep working; a legacy match
    yields the LEGACY sentinel principal which has no target access.

    Both code paths are evaluated regardless of which matches, so the
    wall-clock cost does not depend on whether a scope happens to win.
    """
    from caller_scopes import LEGACY_SCOPE_NAME, token_fingerprint  # local import

    header = request.headers.get("authorization", "")
    provided = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not provided:
        # Still walk every comparison so an empty header takes the same
        # wall-clock cost as a malformed one.
        if scope_registry is not None:
            scope_registry.identify_token(provided)
        hmac.compare_digest("", primary)
        hmac.compare_digest("", previous or "")
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    scope_principal = (
        scope_registry.identify_token(provided)
        if scope_registry is not None
        else None
    )
    matches_primary = hmac.compare_digest(provided, primary)
    matches_previous = hmac.compare_digest(provided, previous or "")

    if scope_principal is not None:
        return scope_principal
    if matches_primary or (previous and matches_previous):
        return Principal(
            scope_name=LEGACY_SCOPE_NAME,
            token_fingerprint=token_fingerprint(provided),
            allowed_target_ids=frozenset(),
        )
    raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")


async def _read_request(request: Request, max_bytes: int) -> AgentInvokeRequest:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="Request body exceeds configured limit")
        chunks.append(chunk)
    try:
        raw = json.loads(b"".join(chunks))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Malformed JSON body")
    try:
        return AgentInvokeRequest.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_input=False))


def _build_prompt(
    body: AgentInvokeRequest,
    *,
    live_tools_available: bool | None = None,
) -> str:
    input_json = json.dumps(body.input, sort_keys=True, ensure_ascii=False, default=str)
    context_json = json.dumps(body.context, sort_keys=True, ensure_ascii=False, default=str)
    if live_tools_available is None:
        live_tools_available = body.target is not None
    live_instruction = (
        "\nA server-managed remote Kubernetes target was requested. Gather relevant "
        "current cluster evidence with read-only tools before the final "
        "diagnosis. Do not execute remediation or mutate the cluster.\n"
        if live_tools_available
        else ""
    )
    return (
        f"{body.instruction.strip()}\n{live_instruction}\n"
        f"Context JSON:\n{context_json}\n\n"
        f"Input JSON:\n{input_json}"
    )


def _build_error_only_text(body: AgentInvokeRequest) -> str:
    """Build classifier text without flattening structured request evidence."""
    message_keys = {
        "msg",
        "message",
        "error",
        "stderr",
        "module_stderr",
        "exception",
    }
    messages: list[str] = []

    def _collect(value: Any, *, depth: int = 0) -> None:
        if depth > 12 or len(messages) >= 20:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in message_keys and isinstance(item, str):
                    text = item.strip()
                    if text and text not in messages:
                        messages.append(text[:8000])
                elif isinstance(item, (dict, list)):
                    _collect(item, depth=depth + 1)
        elif isinstance(value, list):
            for item in value:
                _collect(item, depth=depth + 1)

    _collect(body.input)
    if messages:
        input_text = "\n".join(messages)
    elif isinstance(body.input, str):
        input_text = body.input
    else:
        input_text = json.dumps(
            body.input,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
    context_json = json.dumps(
        body.context,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return (
        f"{body.instruction.strip()}\n\n"
        f"Context JSON:\n{context_json}\n\n"
        f"Error text:\n{input_text}"
    )


@dataclass(frozen=True, slots=True)
class _RemoteExecution:
    target: Any
    caller_scope_name: str
    known_hosts_path: str
    connection_timeout_seconds: float
    diagnostic_timeout_seconds: float
    breaker: Any


@dataclass(frozen=True, slots=True)
class _AgentExecutionOutcome:
    result: Any
    diagnostic_mode: DiagnosticMode
    connection: ConnectionInfo


def _read_only_machine_tools() -> set[str]:
    """Return the server-owned tool policy for automated live diagnostics."""
    from tool_registry import tools_for_surface

    allowed_categories = {
        "investigation",
        "discovery",
        "pod",
        "cluster",
        "helm",
        "ai",
    }
    allowed: set[str] = set()
    for tool in tools_for_surface("react"):
        if (
            tool.react_enabled
            and not tool.write_op
            and tool.category in allowed_categories
        ):
            allowed.add(tool.name)
            allowed.update(tool.aliases)
    if not allowed:
        # react_loop treats an empty set as "no scope". Fail closed rather
        # than accidentally exposing the full registry after a registry
        # refactor or import/configuration error.
        raise RuntimeError("machine read-only tool policy resolved to an empty set")
    return allowed


def _error_only_result(
    body: AgentInvokeRequest,
    *,
    reason: ConnectionReason | None = None,
) -> Any:
    """Analyze only caller-supplied input; never dispatch a live-state tool."""
    prompt = _build_error_only_text(body)
    params: dict[str, Any] = {
        "error_text": prompt,
        "diagnostic_mode": "error_only",
    }
    if isinstance(body.input, dict):
        params["structured_payload"] = body.input
    result = chat._dispatch(
        "analyze_error",
        params,
        surface="chat",
    )
    if not isinstance(result, dict) or result.get("error"):
        connection_note = (
            f" Live cluster diagnostics could not be completed ({reason})."
            if reason
            else ""
        )
        return chat.ChatResponse(
            reply=(
                "The supplied error could not be fully analyzed."
                f"{connection_note} Review the request payload and target configuration."
            ),
            tool_used="analyze_error",
            result={"connection_reason": reason} if reason else {},
            timestamp=time.time(),
            suggested_actions=[],
        )

    root_cause = str(result.get("root_cause") or "").strip()
    solution = str(result.get("solution") or "").strip()
    if root_cause and solution:
        reply = f"{root_cause}\n\nRecommended action: {solution}"
    else:
        reply = root_cause or solution or chat._friendly_summary(
            "analyze_error", result, "Error-only analysis completed."
        )
    images = (result.get("request_evidence") or {}).get("images") or []
    if images:
        image_lines = "\n".join(f"- `{image}`" for image in images[:10])
        reply = f"{reply}\n\nImages from the request payload:\n{image_lines}"
    return chat.ChatResponse(
        reply=reply,
        tool_used="analyze_error",
        result=result,
        timestamp=time.time(),
        suggested_actions=result.get("steps") or [],
    )


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.perf_counter()
    if remaining <= 0:
        raise TimeoutError("agent execution deadline expired")
    return remaining


def _request_timeout_arg(deadline: float) -> str:
    # Floor to whole milliseconds so kubectl never receives a timeout longer
    # than the worker's remaining cumulative budget.
    remaining_ms = int(_remaining_seconds(deadline) * 1000)
    if remaining_ms < 1:
        raise TimeoutError("insufficient time remains for kubectl preflight")
    return f"--request-timeout={remaining_ms}ms"


def _verify_cluster_identity(runner: Any, expected_uid: str, deadline: float) -> tuple[str, str]:
    """Run the bounded Kubernetes preflight and return (context, UID)."""
    from k8s.kubectl_runner import KubectlError, KubectlTimeoutError
    from k8s.remote_connection import RemoteConnectionError

    try:
        runner.run_json([_request_timeout_arg(deadline), "version"])
        context_result = runner.run(
            [_request_timeout_arg(deadline), "config", "current-context"],
            max_output=4096,
        )
        context_result.raise_for_status()
        namespace = runner.run_json(
            [_request_timeout_arg(deadline), "get", "namespace", "kube-system"]
        )
        uid = str((namespace.get("metadata") or {}).get("uid") or "").lower()
    except (TimeoutError, KubectlTimeoutError, KubectlError):
        raise RemoteConnectionError("transport_error") from None

    if not uid or uid != expected_uid.lower():
        raise RemoteConnectionError("identity_mismatch")
    return context_result.stdout.strip(), uid


def _create_ssh_runner(remote: _RemoteExecution, timeout: float):
    """Factory seam used by tests; request data never reaches this constructor."""
    from k8s.ssh_runner import SSHKubectlRunner

    target = remote.target
    return SSHKubectlRunner(
        host=target.host,
        port=target.port,
        username=target.username,
        credential_path=target.credential_path,
        known_hosts_path=remote.known_hosts_path,
        known_hosts_alias=target.known_hosts_alias,
        timeout=timeout,
    )


def _connection_metric(target_id: str, status: str, reason: str = "") -> None:
    try:
        from metrics import agent_remote_connection_attempts_total
        agent_remote_connection_attempts_total.labels(
            target_id=target_id,
            status=status,
            reason=reason,
        ).inc()
    except Exception:
        pass


def _hallucinated_evidence_claims(answer: str) -> list[str]:
    return [
        category
        for category, pattern in _ERROR_ONLY_LIVE_CLAIM_PATTERNS
        if pattern.search(answer or "")
    ]


def _emit_agent_audit_event(event: str, **fields: Any) -> None:
    """Append a metadata-only agent event to the shared audit log."""
    try:
        from config.settings import get_settings
        from k8s.kubectl_runner import _append_audit_entry

        settings = get_settings()
        if not settings.enable_audit_log:
            return
        parts = [datetime.now(timezone.utc).isoformat(), event]
        for key, value in fields.items():
            safe_value = _redact(value)
            if isinstance(safe_value, (dict, list)):
                rendered = json.dumps(
                    safe_value,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
            else:
                rendered = str(safe_value)
            rendered = " ".join(rendered.split())[:512]
            parts.append(f"{key}={rendered}")
        _append_audit_entry(
            Path(settings.audit_log_path),
            " | ".join(parts),
            settings.audit_log_max_bytes,
        )
    except Exception as exc:
        # Never include field values here; they may originate from the request.
        logger.warning(
            "agent_audit_event_failed event=%s error_type=%s",
            event,
            type(exc).__name__,
        )
        logger.debug("agent audit event traceback", exc_info=True)


def _connection_duration_metric(target_id: str, started: float) -> float:
    duration = time.perf_counter() - started
    try:
        from metrics import agent_remote_connection_duration_seconds
        agent_remote_connection_duration_seconds.labels(
            target_id=target_id
        ).observe(duration)
    except Exception:
        pass
    return duration


def _circuit_state_metric(remote: _RemoteExecution) -> None:
    try:
        from metrics import agent_remote_circuit_state

        state, _ = remote.breaker.snapshot(remote.target.target_id)
        value = {"closed": 0, "half_open": 1, "open": 2}[state]
        agent_remote_circuit_state.labels(target_id=remote.target.target_id).set(value)
    except Exception:
        pass


def _execute_remote_agent(
    body: AgentInvokeRequest,
    deadline: float,
    remote: _RemoteExecution,
) -> _AgentExecutionOutcome:
    from k8s.kubectl_runner import runner_ctx, set_runner
    from k8s.remote_connection import RemoteConnectionError

    target = remote.target
    target_id = target.target_id
    decision = remote.breaker.allow(target_id)
    _circuit_state_metric(remote)
    if not decision.allowed:
        _connection_metric(target_id, "failure", "circuit_open")
        try:
            from metrics import agent_remote_circuit_open_total
            agent_remote_circuit_open_total.labels(target_id=target_id).inc()
        except Exception:
            pass
        return _AgentExecutionOutcome(
            result=_error_only_result(body, reason="circuit_open"),
            diagnostic_mode="error_only",
            connection=ConnectionInfo(
                type="ssh",
                target_id=target_id,
                verified=False,
                reason="circuit_open",
            ),
        )

    runner = None
    runner_token = None
    connection_started = time.perf_counter()
    probe_resolved = False
    try:
        connection_timeout = min(
            remote.connection_timeout_seconds,
            _remaining_seconds(deadline),
        )
        runner = _create_ssh_runner(remote, connection_timeout)
        runner.connect()

        diagnostic_deadline = min(
            deadline,
            time.perf_counter() + remote.diagnostic_timeout_seconds,
        )
        runner.timeout = min(
            remote.diagnostic_timeout_seconds,
            _remaining_seconds(deadline),
        )
        # SSHKubectlRunner.set_operation_deadline is documented in the
        # ``time.monotonic()`` domain, but ``deadline`` here is a
        # ``time.perf_counter()`` value (that is what the request handler and
        # ReAct loop use). The two clocks are not guaranteed to share an
        # origin — on macOS they can differ by thousands of seconds — so
        # translate to the monotonic epoch before installing the runner
        # budget. Falling below zero after translation means the request
        # deadline is already gone; raise the standard timeout.
        diagnostic_deadline_monotonic = time.monotonic() + max(
            0.0, diagnostic_deadline - time.perf_counter()
        )
        if diagnostic_deadline_monotonic <= time.monotonic():
            raise TimeoutError("insufficient time remains for remote diagnostics")
        runner.set_operation_deadline(diagnostic_deadline_monotonic)
        _context_name, kube_system_uid = _verify_cluster_identity(
            runner,
            target.expected_kube_system_uid,
            diagnostic_deadline,
        )

        # The breaker protects connection + identity verification only.
        # Resolve the half-open probe before LLM/tool synthesis so a later
        # analysis failure cannot wedge the target.
        remote.breaker.record_success(
            target_id, probe_token=decision.probe_token
        )
        probe_resolved = True
        _connection_metric(target_id, "success")
        _circuit_state_metric(remote)
        connection_duration = _connection_duration_metric(
            target_id, connection_started
        )

        # Install only after host-key, auth, apiserver, and kube-system UID
        # verification have all succeeded. Before this point error handling
        # uses _error_only_result and cannot reach get_runner()'s local default.
        runner_token = set_runner(runner)
        req = chat.ChatRequest(message=_build_prompt(body), history=[])
        provider = chat._llm_provider(None)
        persist = lambda *args, **kwargs: None
        if provider is not None and provider.enabled:
            result = chat._chat_react(
                req,
                provider,
                persist,
                "machine",
                route="agent_invoke_remote",
                capture_enabled=False,
                # The runner enforces the shorter cumulative remote-command
                # budget. ReAct keeps the full request deadline so it can
                # synthesize partial evidence after remote calls stop.
                deadline_monotonic=deadline,
                tool_scope_override=_read_only_machine_tools(),
            )
        else:
            # The keyword fallback is still safe: the verified SSH runner is
            # installed, and registry dispatch rejects write tools on the chat
            # surface. The response will be marked partial if it gathers no
            # live evidence.
            result = chat._chat_single_shot(
                req,
                persist,
                "machine",
                tool_scope_override=_read_only_machine_tools(),
            )

        return _AgentExecutionOutcome(
            result=result,
            diagnostic_mode="live_cluster_partial",
            connection=ConnectionInfo(
                type="ssh",
                target_id=target_id,
                verified=True,
                connected_host=target.host,
                kube_system_uid=kube_system_uid,
                duration_ms=round(connection_duration * 1000),
            ),
        )
    except RemoteConnectionError as exc:
        _connection_metric(target_id, "failure", exc.reason)
        connection_duration = _connection_duration_metric(
            target_id, connection_started
        )
        if not probe_resolved:
            # Pre-verification failure. Counts against the breaker and the
            # response must not claim a verified live investigation.
            remote.breaker.record_failure(
                target_id,
                exc.reason,
                probe_token=decision.probe_token,
            )
            _circuit_state_metric(remote)
            return _AgentExecutionOutcome(
                result=_error_only_result(body, reason=exc.reason),
                diagnostic_mode="error_only",
                connection=ConnectionInfo(
                    type="ssh",
                    target_id=target_id,
                    verified=False,
                    reason=exc.reason,
                    duration_ms=round(connection_duration * 1000),
                ),
            )
        # Post-verification: identity preflight already succeeded, so the
        # connection was truthfully live. A subsequent remote failure (e.g.
        # a reconnect during a ReAct tool call) should not roll the response
        # back to error_only — that would drop any evidence already gathered
        # and lie about whether the cluster identity was verified. Keep the
        # verified live state and mark it partial. Reason is intentionally
        # omitted: ConnectionReason on a verified/live response is null by
        # the response validator's contract.
        _circuit_state_metric(remote)
        return _AgentExecutionOutcome(
            result=chat.ChatResponse(
                reply=(
                    "The remote cluster identity was verified, but the "
                    "diagnostic analysis did not complete."
                ),
                tool_used="none",
                result=None,
                error="remote_transport_failed_after_verification",
                timestamp=time.time(),
                suggested_actions=[],
            ),
            diagnostic_mode="live_cluster_partial",
            connection=ConnectionInfo(
                type="ssh",
                target_id=target_id,
                verified=True,
                connected_host=target.host,
                kube_system_uid=target.expected_kube_system_uid,
                duration_ms=round(connection_duration * 1000),
            ),
        )
    except Exception:
        # ``Exception`` is intentional: it catches every anticipated failure
        # (ValueError, KubectlError, provider errors) without swallowing
        # ``KeyboardInterrupt`` / ``SystemExit`` / ``asyncio.CancelledError``
        # (all ``BaseException`` subclasses in Python 3.8+), which must
        # continue to propagate for graceful shutdown and disconnect handling.
        # Before verified preflight this is a transport failure. After
        # verification it is an analysis/tool failure and the connection is
        # still truthful, so preserve live-partial mode.
        if not probe_resolved:
            remote.breaker.record_failure(
                target_id,
                "transport_error",
                probe_token=decision.probe_token,
            )
            _connection_metric(target_id, "failure", "transport_error")
            _circuit_state_metric(remote)
            connection_duration = _connection_duration_metric(
                target_id, connection_started
            )
            return _AgentExecutionOutcome(
                result=_error_only_result(body, reason="transport_error"),
                diagnostic_mode="error_only",
                connection=ConnectionInfo(
                    type="ssh",
                    target_id=target_id,
                    verified=False,
                    reason="transport_error",
                    duration_ms=round(connection_duration * 1000),
                ),
            )
        return _AgentExecutionOutcome(
            result=chat.ChatResponse(
                reply=(
                    "The remote cluster identity was verified, but the "
                    "diagnostic analysis did not complete."
                ),
                tool_used="none",
                result=None,
                error="diagnostic_analysis_failed",
                timestamp=time.time(),
                suggested_actions=[],
            ),
            diagnostic_mode="live_cluster_partial",
            connection=ConnectionInfo(
                type="ssh",
                target_id=target_id,
                verified=True,
                connected_host=target.host,
                kube_system_uid=target.expected_kube_system_uid,
                duration_ms=round(connection_duration * 1000),
            ),
        )
    finally:
        try:
            if runner_token is not None:
                runner_ctx.reset(runner_token)
        finally:
            # Context restoration and transport cleanup are independent
            # invariants. A reset failure must never leak the SSH connection.
            if runner is not None:
                runner.close()


def _execute_agent(
    body: AgentInvokeRequest,
    deadline: float,
    remote: _RemoteExecution | None = None,
):
    from services.llm.base import (
        reset_generation_deadline,
        set_generation_deadline,
    )

    # Translate the ReAct perf_counter deadline to the monotonic clock used by
    # provider timeout enforcement instead of assuming their epochs match.
    llm_deadline = time.monotonic() + _remaining_seconds(deadline)
    llm_deadline_token = set_generation_deadline(llm_deadline)
    try:
        if remote is not None:
            return _execute_remote_agent(body, deadline, remote)

        # No target means no live-cluster authority. Keep this path on the
        # input-only analyzer even when an LLM provider is enabled; the general
        # ReAct loop can dispatch Kubernetes tools via the local default runner.
        return _error_only_result(body)
    finally:
        reset_generation_deadline(llm_deadline_token)


def _evidence_from_steps(
    raw_steps: list[dict[str, Any]],
    *,
    target_id: str,
) -> list[Evidence]:
    from tool_registry import resolve_tool

    observed_at = datetime.now(timezone.utc).isoformat()
    evidence: list[Evidence] = []
    for step in raw_steps:
        if step.get("step_kind") != "tool" or step.get("status") != "ok":
            continue
        action = str(step.get("action") or "")
        tool = resolve_tool(action)
        if (
            tool is None
            or tool.write_op
            or tool.category not in _LIVE_EVIDENCE_CATEGORIES
        ):
            continue
        preview = _redact(str(step.get("observation_preview") or "").strip())
        if not preview:
            continue
        evidence.append(
            Evidence(
                source="kubernetes",
                tool=tool.name,
                summary=str(preview)[:1200],
                target_id=target_id,
                observed_at=observed_at,
            )
        )
    return evidence[:20]


def create_router(
    *,
    primary_token: str,
    previous_token: str | None = None,
    max_concurrency: int = 2,
    queue_timeout_seconds: float = 5.0,
    max_input_bytes: int = 65536,
    requests_per_minute: int = 30,
    execution_timeout_seconds: float = 90.0,
    remote_diagnostics_enabled: bool = False,
    scope_registry: "ScopeRegistry | None" = None,
    target_registry: Any = None,
    known_hosts_path: str = "",
    connection_timeout_seconds: float = 10.0,
    diagnostic_timeout_seconds: float = 30.0,
    circuit_breaker: Any = None,
) -> APIRouter:
    for name, value in (
        ("execution_timeout_seconds", execution_timeout_seconds),
        ("connection_timeout_seconds", connection_timeout_seconds),
        ("diagnostic_timeout_seconds", diagnostic_timeout_seconds),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if not math.isfinite(queue_timeout_seconds) or queue_timeout_seconds < 0:
        raise ValueError(
            "queue_timeout_seconds must be finite and non-negative"
        )
    if circuit_breaker is None:
        from remote_diagnostics_breaker import RemoteCircuitBreaker
        circuit_breaker = RemoteCircuitBreaker(
            probe_lease_seconds=max(120.0, execution_timeout_seconds + 30.0)
        )

    router = APIRouter(prefix="/api/v1/agent", tags=["agent"])
    limiter = RateLimiter()
    execution_slots = threading.BoundedSemaphore(max(1, max_concurrency))

    @router.post("/invoke", response_model=AgentInvokeResponse)
    async def invoke(request: Request):
        request_started = time.perf_counter()
        principal = _parse_bearer(
            request, primary_token, previous_token, scope_registry
        )
        fingerprint = principal.token_fingerprint
        if not limiter.allow(fingerprint, max(1, requests_per_minute), 60):
            from metrics import agent_invoke_rejections_total
            agent_invoke_rejections_total.labels(reason="rate_limit").inc()
            raise HTTPException(
                status_code=429,
                detail="Agent API rate limit exceeded",
                headers={"Retry-After": "60"},
            )

        try:
            body = await _read_request(request, max_input_bytes)
        except HTTPException as exc:
            try:
                from metrics import agent_invoke_rejections_total
                reason = "size" if exc.status_code == 413 else "invalid_request"
                agent_invoke_rejections_total.labels(reason=reason).inc()
            except Exception:
                pass
            raise
        except BaseException:
            # Client disconnect, transport error, or cancellation during body
            # read. Categorize separately so disconnects do not silently vanish
            # from request metrics.
            try:
                from metrics import agent_invoke_rejections_total
                agent_invoke_rejections_total.labels(reason="body_read_failed").inc()
            except Exception:
                pass
            raise

        # Deployment kill switch. Target-less machine invocation remains
        # available independently.
        if body.target is not None and (
            not _TARGET_RESOLUTION_IMPLEMENTED or not remote_diagnostics_enabled
        ):
            try:
                from metrics import agent_invoke_rejections_total
                agent_invoke_rejections_total.labels(reason="remote_disabled").inc()
            except Exception:
                pass
            raise HTTPException(
                status_code=501,
                detail="Remote diagnostics is not enabled on this backend",
            )

        # Authorization is deliberately before capacity acquisition: invalid
        # target requests fail fast and cannot consume an execution slot.
        remote_execution: _RemoteExecution | None = None
        if body.target is not None:
            from caller_scopes import AuthorizationError, authorize_target
            from target_registry import get_active as get_active_targets

            registry = (
                target_registry
                if target_registry is not None
                else get_active_targets()
            )
            try:
                resolved_target = authorize_target(
                    principal,
                    body.target.target_id,
                    registry,
                )
                if not body.target.diagnostic_scope.issubset(
                    resolved_target.diagnostic_scopes_allowed
                ):
                    raise AuthorizationError(
                        http_status=403,
                        reason="unauthorized",
                        detail="requested diagnostic scope is not allowed for target",
                    )
            except AuthorizationError as exc:
                try:
                    from metrics import agent_invoke_rejections_total
                    agent_invoke_rejections_total.labels(reason=exc.reason).inc()
                except Exception:
                    pass
                # Unknown IDs are caller-controlled. Never copy them into a
                # Prometheus label; registered target IDs are bounded by the
                # startup-loaded registry and are safe label values.
                metric_target_id = (
                    body.target.target_id
                    if registry.get(body.target.target_id) is not None
                    else _UNKNOWN_TARGET_METRIC_LABEL
                )
                _connection_metric(metric_target_id, "failure", exc.reason)
                raise HTTPException(
                    status_code=exc.http_status,
                    detail=exc.detail,
                )
            if not known_hosts_path:
                raise HTTPException(
                    status_code=503,
                    detail="Remote diagnostic trust configuration is unavailable",
                )
            remote_execution = _RemoteExecution(
                target=resolved_target,
                caller_scope_name=principal.scope_name,
                known_hosts_path=known_hosts_path,
                connection_timeout_seconds=connection_timeout_seconds,
                diagnostic_timeout_seconds=diagnostic_timeout_seconds,
                breaker=circuit_breaker,
            )

        # Non-blocking acquire + async poll. Avoids parking FastAPI threadpool
        # threads while waiting for a slot — a blocking acquire on a 2-slot
        # semaphore would otherwise hold one threadpool worker per queued
        # request for up to queue_timeout_seconds.
        queue_started = time.perf_counter()
        queue_deadline = queue_started + queue_timeout_seconds
        acquired = execution_slots.acquire(blocking=False)
        while not acquired and time.perf_counter() < queue_deadline:
            await asyncio.sleep(0.05)
            acquired = execution_slots.acquire(blocking=False)
        if not acquired:
            from metrics import agent_invoke_queue_wait_seconds, agent_invoke_rejections_total
            agent_invoke_queue_wait_seconds.observe(time.perf_counter() - queue_started)
            agent_invoke_rejections_total.labels(reason="concurrency").inc()
            raise HTTPException(
                status_code=429,
                detail="Agent execution capacity is currently full",
                headers={"Retry-After": str(max(1, int(queue_timeout_seconds)))},
            )

        # Everything after acquire() must run under the slot-release finally
        # below. A failure between acquire and the inner try (metrics import,
        # task creation, etc.) would otherwise leak a slot.
        try:
            from metrics import (
                agent_invoke_active_requests,
                agent_invoke_duration_seconds,
                agent_invoke_failures_total,
                agent_invoke_queue_wait_seconds,
                agent_invoke_requests_total,
            )
            agent_invoke_queue_wait_seconds.observe(time.perf_counter() - queue_started)
            agent_invoke_active_requests.inc()
            deadline = time.perf_counter() + execution_timeout_seconds
            if remote_execution is None:
                worker_call = run_in_threadpool(_execute_agent, body, deadline)
            else:
                worker_call = run_in_threadpool(
                    _execute_agent,
                    body,
                    deadline,
                    remote_execution,
                )
            worker = asyncio.create_task(worker_call)
            # Shield keeps cancellation from releasing capacity while the
            # underlying thread continues. On disconnect, wait for the worker
            # to finish before releasing the semaphore.
            try:
                result = await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Keep the slot held until the worker actually exits. Swallow
                # any worker-raised exception so it does not mask the original
                # CancelledError on the disconnect path.
                try:
                    await worker
                except BaseException:
                    pass
                raise

            outcome = (
                result
                if isinstance(result, _AgentExecutionOutcome)
                else _AgentExecutionOutcome(
                    result=result,
                    diagnostic_mode="error_only",
                    connection=ConnectionInfo(),
                )
            )
            result = outcome.result
            raw_steps = db.get_agent_steps(result.run_id) if result.run_id else []
            steps = [
                AgentInvokeStep(
                    action=str(step.get("action") or ""),
                    status=str(step.get("status") or ""),
                    duration_ms=step.get("duration_ms"),
                    params=_safe_step_params(step.get("params_json")),
                )
                for step in raw_steps
                if step.get("step_kind") == "tool"
            ]
            evidence: list[Evidence] = []
            diagnostic_mode = outcome.diagnostic_mode
            if outcome.connection.verified and outcome.connection.target_id:
                evidence = _evidence_from_steps(
                    raw_steps,
                    target_id=outcome.connection.target_id,
                )
                failed_tool = any(
                    step.get("step_kind") == "tool"
                    and step.get("status") != "ok"
                    for step in raw_steps
                )
                diagnostic_mode = (
                    "live_cluster"
                    if evidence and not failed_tool and not result.error
                    else "live_cluster_partial"
                )
            request_id = getattr(request.state, "request_id", "")
            elapsed_ms = round((time.perf_counter() - request_started) * 1000)
            answer = _redact(result.reply)
            error = (
                {"type": "agent_execution", "message": _redact(result.error), "retryable": False}
                if result.error else None
            )
            if diagnostic_mode == "error_only" and not evidence:
                hallucinated_claims = _hallucinated_evidence_claims(str(answer))
                if hallucinated_claims:
                    _emit_agent_audit_event(
                        "hallucinated_evidence",
                        request_id=request_id,
                        run_id=result.run_id or "none",
                        token_fingerprint=fingerprint,
                        target_id=outcome.connection.target_id or "none",
                        diagnostic_mode=diagnostic_mode,
                        claim_categories=hallucinated_claims,
                    )
            status = "failed" if error else "completed"
            agent_invoke_requests_total.labels(status=status).inc()
            if error:
                agent_invoke_failures_total.labels(type="agent_execution").inc()
            if outcome.connection.target_id:
                try:
                    from metrics import agent_remote_diagnostic_mode_total
                    agent_remote_diagnostic_mode_total.labels(
                        target_id=outcome.connection.target_id,
                        mode=diagnostic_mode,
                    ).inc()
                except Exception:
                    pass
            return AgentInvokeResponse(
                request_id=request_id,
                run_id=result.run_id,
                status=status,
                diagnostic_mode=diagnostic_mode,
                connection=outcome.connection,
                answer=answer,
                tool_used=result.tool_used,
                tool_result=_redact(result.result),
                steps=steps,
                evidence=evidence,
                suggested_actions=_redact(result.suggested_actions),
                timing_ms=elapsed_ms,
                error=error,
            )
        except asyncio.CancelledError:
            try:
                agent_invoke_requests_total.labels(status="disconnected").inc()
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                agent_invoke_requests_total.labels(status="failed").inc()
                agent_invoke_failures_total.labels(type=type(exc).__name__).inc()
            except Exception:
                pass
            raise HTTPException(status_code=500, detail="Agent invocation failed")
        finally:
            try:
                agent_invoke_active_requests.dec()
                agent_invoke_duration_seconds.observe(time.perf_counter() - request_started)
            except Exception:
                pass
            # Unconditional — protects the slot even if metrics import or
            # task creation above raised before the inner blocks ran.
            execution_slots.release()

    return router


def router_from_environment() -> APIRouter | None:
    """Build the agent invoke router from environment variables.

    The route is registered if EITHER:
      * the legacy ``AGENT_API_TOKEN`` is set (existing target-less callers), OR
      * a caller-scopes file is set via ``AGENT_API_CALLER_SCOPES_PATH`` and
        contains at least one valid scope.

    Scoped-only deployments — i.e. ones that prefer not to maintain a global
    unscoped target-less token — can leave ``AGENT_API_TOKEN`` unset and rely
    entirely on the per-scope tokens.
    """
    primary = os.environ.get("AGENT_API_TOKEN", "").strip()
    previous = os.environ.get("AGENT_API_TOKEN_PREVIOUS") or None

    # Orphan-previous guard: rotation needs a current. AGENT_API_TOKEN_PREVIOUS
    # without AGENT_API_TOKEN is always a misconfiguration — letting it through
    # would (a) accept the orphan as a legacy bearer (bypassing the scoped-only
    # contract) and (b) skip the scope registry's duplicate-token detection
    # since no legacy CallerScope was created. Fail loudly at startup so the
    # operator sees the issue immediately.
    if previous and not primary:
        raise RuntimeError(
            "AGENT_API_TOKEN_PREVIOUS is set without AGENT_API_TOKEN. Either "
            "configure AGENT_API_TOKEN as well (for normal current/previous "
            "rotation) or unset AGENT_API_TOKEN_PREVIOUS to run scoped-only."
        )

    # Load caller-scope registry first; legacy may be absent.
    scope_registry = None
    scopes_path = os.environ.get("AGENT_API_CALLER_SCOPES_PATH", "").strip()
    if scopes_path:
        from caller_scopes import (
            DEFAULT_TOKEN_MOUNT_ROOT,
            load_registry as _load_scopes,
            set_active as _set_active_scopes,
        )
        token_mount_root = os.environ.get(
            "AGENT_API_TOKEN_MOUNT_ROOT", DEFAULT_TOKEN_MOUNT_ROOT
        )
        scope_registry = _load_scopes(
            scopes_path,
            token_mount_root=token_mount_root,
            legacy_current_token=primary or None,
            legacy_previous_token=previous,
        )
        _set_active_scopes(scope_registry)

    has_legacy = bool(primary)
    has_scopes = scope_registry is not None and len(scope_registry.scopes) > 0
    if not has_legacy and not has_scopes:
        return None

    remote_diagnostics_enabled = (
        os.environ.get("AGENT_API_REMOTE_DIAGNOSTICS_ENABLED", "false")
        .strip()
        .lower()
        in ("1", "true", "yes", "on")
    )
    active_target_registry = None
    known_hosts_path = os.environ.get("SSH_KNOWN_HOSTS_PATH", "").strip()
    if remote_diagnostics_enabled:
        targets_path = os.environ.get("AGENT_API_TARGETS_PATH", "").strip()
        if not targets_path:
            raise RuntimeError(
                "AGENT_API_TARGETS_PATH is required when remote diagnostics is enabled"
            )
        if not os.path.isfile(targets_path):
            raise RuntimeError(
                "AGENT_API_TARGETS_PATH does not reference a readable registry file"
            )
        if not known_hosts_path or not os.path.isfile(known_hosts_path):
            raise RuntimeError(
                "SSH_KNOWN_HOSTS_PATH must reference a readable pinned-host-key "
                "file when remote diagnostics is enabled"
            )
        from target_registry import (
            DEFAULT_CREDENTIAL_ROOT,
            load_registry as _load_targets,
            set_active as _set_active_targets,
        )

        active_target_registry = _load_targets(
            targets_path,
            credential_root=os.environ.get(
                "AGENT_API_TARGET_CREDENTIAL_ROOT",
                DEFAULT_CREDENTIAL_ROOT,
            ),
        )
        if len(active_target_registry) == 0:
            raise RuntimeError(
                "Remote diagnostics is enabled but the target registry is empty"
            )
        _set_active_targets(active_target_registry)

    return create_router(
        # Pass an empty string when there is no legacy token. ``_parse_bearer``
        # will still call hmac.compare_digest against it for timing parity,
        # but no legitimate caller can produce a match against "".
        primary_token=primary or "",
        previous_token=previous,
        max_concurrency=int(os.environ.get("AGENT_API_MAX_CONCURRENCY", "2")),
        queue_timeout_seconds=float(os.environ.get("AGENT_API_QUEUE_TIMEOUT_SECONDS", "5")),
        max_input_bytes=int(os.environ.get("AGENT_API_MAX_INPUT_BYTES", "65536")),
        requests_per_minute=int(os.environ.get("AGENT_API_REQUESTS_PER_MINUTE", "30")),
        execution_timeout_seconds=float(os.environ.get("AGENT_API_EXECUTION_TIMEOUT_SECONDS", "90")),
        remote_diagnostics_enabled=remote_diagnostics_enabled,
        scope_registry=scope_registry,
        target_registry=active_target_registry,
        known_hosts_path=known_hosts_path,
        connection_timeout_seconds=float(
            os.environ.get("AGENT_API_CONNECTION_TIMEOUT_SECONDS", "10")
        ),
        diagnostic_timeout_seconds=float(
            os.environ.get("AGENT_API_DIAGNOSTIC_PHASE_TIMEOUT_SECONDS", "30")
        ),
    )
