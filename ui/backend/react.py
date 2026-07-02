"""ReAct (Reasoning + Acting) orchestrator for multi-step investigations.

Replaces the single-shot route → dispatch → synthesize pipeline with an
iterative loop where the LLM decides which tools to call, observes the
results, and continues until it has enough information to answer.

Usage:
    from react import react_loop
    result = react_loop(question, history, llm_provider, dispatch_fn)
"""

import json
import logging
import re
import shlex
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agent_run_recorder import fail as _rec_fail
from agent_run_recorder import finish as _rec_finish
from agent_run_recorder import record as _rec_step
from observation_sanitizer import sanitize_observation

try:
    from services.llm.pricing import TokenUsage
except ImportError:  # pragma: no cover - exercised when MCP_PATH isn't on sys.path
    @dataclass
    class TokenUsage:  # type: ignore[no-redef]
        tokens_in: int = 0
        cached_tokens_in: int = 0
        tokens_out: int = 0
        cost_usd: float = 0.0
        model: str = ""

        def __add__(self, other: "TokenUsage") -> "TokenUsage":
            return TokenUsage(
                tokens_in=self.tokens_in + other.tokens_in,
                cached_tokens_in=self.cached_tokens_in + other.cached_tokens_in,
                tokens_out=self.tokens_out + other.tokens_out,
                cost_usd=self.cost_usd + other.cost_usd,
                model=self.model or other.model,
            )

        @classmethod
        def empty(cls, model: str = "") -> "TokenUsage":
            return cls(model=model)


class TracedProviderProxy:
    _TRACED_METHODS = ("generate", "generate_with_usage",
                       "generate_stream", "generate_stream_with_usage")

    def __init__(self, inner, tracer):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_tracer", tracer)
        # Pre-wrap and cache methods to avoid rebuilding them on every access
        cached_wrappers = {}
        for name in self._TRACED_METHODS:
            if hasattr(inner, name):
                attr = getattr(inner, name)
                cached_wrappers[name] = self._wrap(name, attr)
        object.__setattr__(self, "_cached_wrappers", cached_wrappers)

    def __getattr__(self, name):
        if name in self._cached_wrappers:
            return self._cached_wrappers[name]
        return getattr(self._inner, name)

    def _wrap(self, name, original_fn):
        if name == "generate_with_usage":
            def traced_generate_with_usage(prompt, system=None, temperature=0.2, max_tokens=None):
                with self._tracer.start_as_current_span("llm_call") as llm_span:
                    llm_span.set_attribute("model", getattr(self._inner, "model", "unknown"))
                    res = original_fn(prompt, system, temperature, max_tokens)
                    if isinstance(res, tuple) and len(res) == 2:
                        text, usage = res
                        if usage:
                            llm_span.set_attribute("tokens_in", getattr(usage, "tokens_in", 0))
                            llm_span.set_attribute("cached_tokens_in", getattr(usage, "cached_tokens_in", 0))
                            llm_span.set_attribute("tokens_out", getattr(usage, "tokens_out", 0))
                    return res
            return traced_generate_with_usage

        elif name == "generate_stream_with_usage":
            def traced_generate_stream_with_usage(prompt, system=None, temperature=0.2, max_tokens=None):
                llm_span = self._tracer.start_span("llm_call")
                llm_span.set_attribute("model", getattr(self._inner, "model", "unknown"))
                from opentelemetry.context import attach, detach
                from opentelemetry import trace as otel_trace
                llm_token = attach(otel_trace.set_span_in_context(llm_span))
                
                gen, usage_holder = original_fn(prompt, system, temperature, max_tokens)
                
                def _wrap_gen():
                    try:
                        for chunk in gen:
                            yield chunk
                    finally:
                        if usage_holder:
                            usage = usage_holder[0]
                            llm_span.set_attribute("tokens_in", getattr(usage, "tokens_in", 0))
                            llm_span.set_attribute("cached_tokens_in", getattr(usage, "cached_tokens_in", 0))
                            llm_span.set_attribute("tokens_out", getattr(usage, "tokens_out", 0))
                        detach(llm_token)
                        llm_span.end()
                return _wrap_gen(), usage_holder
            return traced_generate_stream_with_usage

        elif name == "generate":
            def traced_generate(prompt, system=None, temperature=0.2, max_tokens=None):
                with self._tracer.start_as_current_span("llm_call") as llm_span:
                    llm_span.set_attribute("model", getattr(self._inner, "model", "unknown"))
                    return original_fn(prompt, system, temperature, max_tokens)
            return traced_generate

        elif name == "generate_stream":
            def traced_generate_stream(prompt, system=None, temperature=0.2, max_tokens=None):
                llm_span = self._tracer.start_span("llm_call")
                llm_span.set_attribute("model", getattr(self._inner, "model", "unknown"))
                from opentelemetry.context import attach, detach
                from opentelemetry import trace as otel_trace
                llm_token = attach(otel_trace.set_span_in_context(llm_span))
                gen = original_fn(prompt, system, temperature, max_tokens)
                def _wrap_gen():
                    try:
                        for chunk in gen:
                            yield chunk
                    finally:
                        detach(llm_token)
                        llm_span.end()
                return _wrap_gen()
            return traced_generate_stream


class UsageTracker:
    """Accumulates LLM usage across one ReAct run.

    Use ``add(usage)`` after every LLM call. ``take_step()`` returns the
    usage that landed since the last step boundary and resets the per-step
    counter — call it right before ``_rec_step`` so each row gets attribution.
    ``total`` exposes the run-level rollup for ``_rec_finish`` / ``_rec_fail``.
    """

    def __init__(self) -> None:
        self.total: TokenUsage = TokenUsage()
        self._step: TokenUsage = TokenUsage()

    def add(self, usage: TokenUsage) -> None:
        if usage is None:
            return
        self.total = self.total + usage
        self._step = self._step + usage

        # Record LLM metrics to Prometheus
        try:
            from metrics import llm_tokens_total, llm_cost_usd_total
            model = usage.model or "unknown"
            
            # Record fresh input tokens
            fresh_in = max(usage.tokens_in - usage.cached_tokens_in, 0)
            if fresh_in > 0:
                llm_tokens_total.labels(model=model, surface="react", direction="in").inc(fresh_in)
            
            # Record cached input tokens
            if usage.cached_tokens_in > 0:
                llm_tokens_total.labels(model=model, surface="react", direction="cached_in").inc(usage.cached_tokens_in)
                
            # Record output tokens
            if usage.tokens_out > 0:
                llm_tokens_total.labels(model=model, surface="react", direction="out").inc(usage.tokens_out)
                
            # Record cost
            if usage.cost_usd > 0:
                llm_cost_usd_total.labels(model=model).inc(usage.cost_usd)
        except Exception:
            pass

    def take_step(self) -> TokenUsage:
        out = self._step
        self._step = TokenUsage()
        return out


def _stream_with_usage(
    provider: Any,
    prompt: str,
    *,
    system: Optional[str],
    temperature: float,
    max_tokens: int,
    on_chunk: Optional[Callable[[str], None]] = None,
) -> tuple[str, TokenUsage]:
    """Stream from ``provider``, accumulate text, return ``(text, usage)``.

    Falls back to the back-compat ``generate_stream`` path (returning empty
    usage) when the provider doesn't implement ``generate_stream_with_usage``.
    """
    text_chunks: list[str] = []
    if hasattr(provider, "generate_stream_with_usage"):
        stream, usage_holder = provider.generate_stream_with_usage(
            prompt, system=system, temperature=temperature, max_tokens=max_tokens
        )
        for chunk in stream:
            if not chunk:
                continue
            text_chunks.append(chunk)
            if on_chunk is not None:
                on_chunk(chunk)
        usage = usage_holder[0] if usage_holder else TokenUsage.empty(
            model=getattr(provider, "model", "")
        )
    else:
        for chunk in provider.generate_stream(
            prompt, system=system, temperature=temperature, max_tokens=max_tokens
        ):
            if not chunk:
                continue
            text_chunks.append(chunk)
            if on_chunk is not None:
                on_chunk(chunk)
        usage = TokenUsage.empty(model=getattr(provider, "model", ""))
    return "".join(text_chunks), usage

try:
    import yaml
except Exception:  # pragma: no cover - PyYAML is expected in backend deps.
    yaml = None

# Known short error codes returned by the tool dispatcher (or the harness
# scope guard). Anything else bubbling through ``result["error"]`` is treated
# as an exception message.
_KNOWN_TOOL_ERROR_CODES = {
    "unknown_tool",
    "tool_unavailable",
    "invalid_params",
    "duplicate_tool_call",
    "tool_out_of_scope",
}

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

MAX_ITERATIONS = 6           # Enough for discover + investigate + answer
MAX_WALL_CLOCK_SECS = 90     # Hard cap on total loop time (LLM + tool calls)
MAX_OBSERVATION_CHARS = 3000  # Truncate tool output to keep context window sane
MAX_CONTEXT_CHARS = 12000    # Total budget for accumulated observations

# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class ReActStep:
    """One iteration of the ReAct loop."""
    iteration: int
    thought: str
    action: str                    # tool name or "answer"
    action_params: dict = field(default_factory=dict)
    observation: str = ""          # tool result (truncated)
    duration_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)
    envelope: Optional[Any] = None


@dataclass
class ReActResult:
    """Final output from the ReAct loop."""
    answer: str                    # The LLM's final synthesized answer
    tool_used: str                 # Last tool that was decisive (for compat)
    result: Optional[dict] = None  # Last tool's raw result (for frontend)
    steps: list[ReActStep] = field(default_factory=list)
    total_iterations: int = 0
    total_duration_ms: float = 0.0
    suggested_actions: list = field(default_factory=list)
    error: Optional[str] = None
    synthesis_breakdown: Optional[dict] = None


# ── Tool descriptions for the ReAct system prompt ────────────────────────────

def _load_tool_descriptions() -> str:
    """Load tool descriptions from the registry so prompts match dispatch."""
    try:
        from tool_registry import build_react_tool_descriptions
        return build_react_tool_descriptions()
    except Exception as exc:
        logger.warning("Could not load registry tool descriptions: %s", exc)
        return "Available tools (call exactly one per step):\n\n- analyze_error(error_text) -- AI diagnosis of a pasted error message"


def _valid_react_tools() -> list[str]:
    try:
        from tool_registry import valid_tool_names
        return valid_tool_names("react")
    except Exception:
        return ["analyze_error"]


TOOL_DESCRIPTIONS = _load_tool_descriptions()


def _sha16(text: str) -> str:
    """Return the first 16 hex chars of SHA-256(text). Used for prompt versioning."""
    import hashlib
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


TOOL_REGISTRY_SHA = _sha16(TOOL_DESCRIPTIONS)


# ── ReAct system prompt ──────────────────────────────────────────────────────

REACT_SYSTEM = """You are an expert Kubernetes troubleshooting agent for the K8s DevOps Assistant. You investigate cluster issues step by step using the tools available to you.

ENTITY-FIRST RULE: If the user's question names a specific entity (a deployment, service, pod, namespace, or node), call that entity's targeted tool DIRECTLY. Do NOT call discovery tools (find_workload, get_namespaces, kb_search) when the entity is already known. Use discovery tools only when the entity is unknown or when broader cluster context is genuinely needed.

Examples:
- "rollout status of the frontend deployment" → call get_rollout_status with the deployment name. Don't call find_workload first.
- "the api service has no endpoints" → call get_endpoints or get_service with the service name. Don't call get_namespaces first.
- "list namespaces" → call get_namespaces (this is the targeted tool for inventory).

## How you work

You follow a Thought → Action → Observation loop:
1. **Think** about what information you need next
2. **Act** by calling a tool to gather that information
3. **Observe** the result and decide if you need more information

When you have enough information to fully answer the user's question, respond with the "answer" action.

## Response format

You MUST respond with valid JSON in exactly one of these two formats:

### To call a tool:
{
  "thought": "I need to check the pod status to understand why it's crashing",
  "action": "investigate_pod",
  "params": {"pod_name": "my-app", "use_ai": true}
}

### To give a final answer:
{
  "thought": "I now have enough information to explain the root cause",
  "action": "answer"
}

## Rules

1. **Answer as soon as you can.** For listing questions ("what pods?", "list services", "get all node labels"), one tool call is enough — call the tool, then answer immediately with what you got. Do NOT investigate individual pods/nodes unless the user explicitly asks to debug, troubleshoot, or inspect one named resource.
2. **Be efficient** — use the most specific tool first. For "why is X crashing?", start with investigate_pod, not get_pods.
3. **Don't repeat tools** with the same parameters — you already have that data.
4. **Cross-reference only when troubleshooting** — if the user asks why something is broken, check dependencies. If they just asked for a listing, answer with the listing.
5. **Namespace discovery** — if you don't know the namespace for a specific named workload, use find_workload first. If the user asks for all namespaces, all non-kube namespaces, cluster-wide pods, or "non kube namespaces", use get_pods with namespace="*" in one call. For non-kube namespace pod listings, use params {"namespace":"*", "exclude_namespace_prefixes":["kube-"]}. Do NOT iterate namespace-by-namespace for listing questions.
6. **Focused pod inventory** — if the user asks for pod labels, images, resource requests/limits, or pod placement/scheduling, use `get_pods` with the matching focused mode: `labels_only`, `images_only`, `resources_only`, or `placement_only`. Use namespace="*" when the user asks cluster-wide or does not name a namespace.
7. **Focused deployment inventory** — if the user asks for a deployment's labels, images, resource requests/limits, or pod template, use `get_deployment` with `labels_only`, `images_only`, `resources_only`, or `template_only`.
8. **Node listing vs node investigation** — if the user asks for all nodes, node roles, node readiness, or cluster-wide node inventory, use `get_nodes`. If the user asks only for node labels, taints, conditions, or addresses, use `get_nodes` with `labels_only`, `taints_only`, `conditions_only`, or `addresses_only`. Use `investigate_node` only when the current user message explicitly names one node and asks for that node's resources/conditions.
9. **Think out loud** — use your `thought` field to thoroughly reason about the problem and your next steps. However, do NOT include the final answer text in the JSON, just use `action: "answer"`.
10. **Max {max_iterations} tool calls** — if you're running out, give the best answer you can with what you have.
11. **Source/config tracing** — when the user asks where something is configured/defined, or to trace the source of a verified root cause, investigate read-only in this order: (1) inspect the workload's mounted ConfigMaps and env references with `investigate_workload` / `list_namespace_resources`; (2) use `search_configmaps` with the exact failing value (e.g. a pinned dependency/plugin version or config string) to find which ConfigMap and key defines it, then `get_configmap` to read that key; (3) for chart/values provenance, use `get_helm_release` (after `list_helm_releases` / `helm_available`) to read the release's Helm values and rendered manifest — Helm values are closer to source than the rendered ConfigMap; (4) if the live config does not explain it, use `kb_search` for the owning source file or runbook. Report exact ConfigMap names, keys, Helm release/chart names, and resource names. Never read Secret values. If no matching source is found, say so plainly — never invent a file path.
12. **Helm-specific prompts** — for prompts about Helm releases, charts, values, or history ("what chart deployed this?", "show the values for X", "why did the helm release fail?"), prefer the read-only Helm tools (`helm_available`, `list_helm_releases`, `get_helm_release`). If Helm is unavailable, fall back to the `helm.sh/chart` / managed-by labels on the resources for best-effort chart provenance, and say so.

{tool_descriptions}"""


# Stable SHAs of the prompts/registry as configured at module load. These are
# bundled into every agent_runs row so historical traces stay attributable to
# the exact prompt version that produced them.
REACT_SYSTEM_SHA = _sha16(REACT_SYSTEM)
SYSTEM_PROMPT_SHA = REACT_SYSTEM_SHA  # Today the system prompt IS REACT_SYSTEM after substitution.


def _record_metrics_and_return(res_obj: ReActResult, run_recorder: Optional[Any]) -> ReActResult:
    try:
        from metrics import react_iterations
        route_name = getattr(run_recorder, "route", None) or "react"
        react_iterations.labels(route=route_name).observe(res_obj.total_iterations)
    except Exception:
        pass
    return res_obj


def _record_tool_dispatch(action: str, duration: float, result: Any) -> None:
    try:
        from metrics import tool_dispatch_duration_seconds, tool_dispatch_total
        status = "error" if (isinstance(result, dict) and result.get("error")) else "success"
        tool_dispatch_duration_seconds.labels(tool=action, status=status).observe(duration)
        tool_dispatch_total.labels(tool=action, status=status).inc()
    except Exception:
        pass


# ── Core loop ────────────────────────────────────────────────────────────────

def react_loop(
    question: str,
    history: list,
    provider: Any,
    dispatch_fn: Callable[[str, dict], dict],
    max_iterations: int = MAX_ITERATIONS,
    on_event: Optional[Callable[[dict], None]] = None,
    memory_preamble: str = "",
    grounded_preamble: str = "",
    is_cancelled: Optional[Callable[[], bool]] = None,
    run_recorder: Optional[Any] = None,
    tool_scope: Optional[set[str]] = None,
    resume_run_id: Optional[str] = None,
    approved_token: Optional[str] = None,
    approver_user_id: Optional[str] = None,
    deadline_monotonic: Optional[float] = None,
) -> ReActResult:
    """Run the ReAct loop: think → act → observe → repeat until answered.

    Args:
        question: The user's question
        history: Recent chat history (list of ChatMessage-like objects)
        provider: An LLMProvider instance (Gemini, Ollama, etc.)
        dispatch_fn: Function that takes (tool, params) and returns a dict
        max_iterations: Safety cap on tool calls
        on_event: Optional callback fired at key moments so callers can
            stream progress (SSE endpoint uses this). Receives dicts with
            a "type" field. Errors in the callback are swallowed so
            progress reporting never affects correctness.

    Returns:
        ReActResult with the final answer and full step trace
    """
    # ── OpenTelemetry Tracing Setup (Phase 5) ─────────────────────────────────
    from opentelemetry import trace as otel_trace
    from opentelemetry.context import attach, detach
    try:
        from tracing import get_tracer
        t = get_tracer()
    except ImportError:
        t = otel_trace.get_tracer("kubeastra")

    span = t.start_span("react_loop")
    ctx_token = attach(otel_trace.set_span_in_context(span))
    iter_tracker = {"span": None, "token": None}

    try:
        # Wrap the provider dynamically in the non-mutating proxy
        traced_provider = TracedProviderProxy(provider, t)

        # Wrap dispatch_fn with a tracing span
        original_dispatch_fn = dispatch_fn
        def traced_dispatch_fn(tool_name: str, params: dict) -> dict:
            with t.start_as_current_span("tool_dispatch") as tool_span:
                tool_span.set_attribute("tool_name", tool_name)
                res = original_dispatch_fn(tool_name, params)
                status = "error" if (isinstance(res, dict) and res.get("error")) else "success"
                tool_span.set_attribute("status", status)
                return res

        return _react_loop_inner(
            question=question,
            history=history,
            provider=traced_provider,
            dispatch_fn=traced_dispatch_fn,
            max_iterations=max_iterations,
            on_event=on_event,
            memory_preamble=memory_preamble,
            grounded_preamble=grounded_preamble,
            is_cancelled=is_cancelled,
            run_recorder=run_recorder,
            tool_scope=tool_scope,
            resume_run_id=resume_run_id,
            approved_token=approved_token,
            approver_user_id=approver_user_id,
            deadline_monotonic=deadline_monotonic,
            iter_tracker=iter_tracker,
            tracer=t,
        )
    except Exception as exc:
        span.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, description=str(exc)))
        span.record_exception(exc)
        raise
    finally:
        if iter_tracker.get("span") is not None:
            try:
                detach(iter_tracker["token"])
                iter_tracker["span"].end()
            except Exception as exc:
                logger.warning("trace iteration cleanup failed: %s", exc)
        try:
            detach(ctx_token)
            span.end()
        except Exception as exc:
            logger.warning("trace loop cleanup failed: %s", exc)


def _react_loop_inner(
    question: str,
    history: list,
    provider: Any,
    dispatch_fn: Callable[[str, dict], dict],
    max_iterations: int = MAX_ITERATIONS,
    on_event: Optional[Callable[[dict], None]] = None,
    memory_preamble: str = "",
    grounded_preamble: str = "",
    is_cancelled: Optional[Callable[[], bool]] = None,
    run_recorder: Optional[Any] = None,
    tool_scope: Optional[set[str]] = None,
    resume_run_id: Optional[str] = None,
    approved_token: Optional[str] = None,
    approver_user_id: Optional[str] = None,
    deadline_monotonic: Optional[float] = None,
    iter_tracker: dict = None,
    tracer: Any = None,
) -> ReActResult:
    def _emit(event: dict) -> None:
        if on_event is None:
            return
        try:
            on_event(event)
        except Exception as exc:
            logger.warning("on_event callback raised (ignored): %s", exc)

    # ``deadline_monotonic`` is a legacy parameter name — the loop compares it
    # against ``time.perf_counter()`` throughout, so callers must pass a value
    # in the perf_counter clock domain. The name is preserved for API
    # compatibility. See routers/agent.py for the equivalent monotonic-clock
    # deadline used by ssh_runner/llm providers.
    loop_start = time.perf_counter()
    effective_deadline = deadline_monotonic or (loop_start + MAX_WALL_CLOCK_SECS)

    from opentelemetry import trace as otel_trace
    from opentelemetry.context import attach, detach
    t = tracer or otel_trace.get_tracer("kubeastra")

    def _end_iteration():
        if iter_tracker is not None and iter_tracker.get("span") is not None:
            try:
                detach(iter_tracker["token"])
                iter_tracker["span"].end()
            except Exception as exc:
                logger.warning("trace iteration cleanup failed inside inner: %s", exc)
            finally:
                iter_tracker["span"] = None
                iter_tracker["token"] = None

    steps: list[ReActStep] = []
    observations: list[str] = []  # Accumulated context for the LLM
    last_tool = "none"
    last_result = None
    primary_tool = None
    primary_result = None
    discovered_failing_pod: Optional[dict] = None
    executed_calls = set()
    usage_tracker = UsageTracker()  # Phase 1A: per-step + per-run token/cost accumulator.

    # Phase 2: Context Manager initialization
    import uuid
    import db
    try:
        from context_manager import ContextManager
        context_mgr = ContextManager(
            run_id=run_recorder.run_id if run_recorder else str(uuid.uuid4()),
            provider=provider
        )
    except Exception as exc:
        logger.warning("Failed to import or initialize ContextManager: %s", exc)
        context_mgr = None

    envelope_observations: list[dict] = []

    # Phase 3: Resume state reconstruction
    resume_action = None
    resume_params = None
    resume_iteration = None
    resume_step_id = None

    if resume_run_id and approved_token:
        try:
            prior_steps = db.get_agent_steps(resume_run_id)
            if prior_steps:
                last_step_db = prior_steps[-1]
                if last_step_db["status"] == "pending_approval":
                    resume_action = last_step_db["action"]
                    params_val = last_step_db["params_json"]
                    if isinstance(params_val, str):
                        resume_params = json.loads(params_val) if params_val else {}
                    elif isinstance(params_val, dict):
                        resume_params = params_val
                    else:
                        resume_params = {}
                    resume_iteration = last_step_db["iteration"]
                    resume_step_id = last_step_db["id"]
                    
                    resume_params["confirm"] = True
                    resume_params["confirmation_token"] = approved_token
                    if "dry_run" in resume_params:
                        del resume_params["dry_run"]
                        
                    prior_steps = prior_steps[:-1]
                    
                for s in prior_steps:
                    action_name = s["action"]
                    params_dict = s["params_json"]
                    if isinstance(params_dict, str):
                        params_dict = json.loads(params_dict) if params_dict else {}
                    elif not isinstance(params_dict, dict):
                        params_dict = {}
                    obs_ref = s["observation_ref"]
                    obs_content = ""
                    if obs_ref:
                        obs_row = db.get_agent_observation(obs_ref)
                        if obs_row:
                            obs_content = obs_row["content"]
                    if not obs_content:
                        obs_content = s["observation_preview"] or ""
                    
                    if context_mgr:
                        envelope = context_mgr.wrap_observation_envelope(action_name, params_dict, obs_content)
                    else:
                        envelope = {"tool": action_name, "observation": obs_content, "trust": "untrusted"}
                    envelope_observations.append(envelope)
                    
                    steps.append(ReActStep(
                        iteration=s["iteration"],
                        thought=s["thought"] or "",
                        action=action_name,
                        action_params=params_dict,
                        observation=s["observation_preview"] or "",
                        duration_ms=s["duration_ms"] or 0.0,
                    ))
                    
                    if _should_replace_primary_result(primary_tool, action_name):
                        primary_tool = action_name
                        primary_result = {"status": s["status"], "observation_preview": s["observation_preview"]}
                        
                    last_tool = action_name
                    try:
                        serialized_params = json.dumps(params_dict, sort_keys=True)
                        executed_calls.add((action_name, serialized_params))
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning("Failed to reconstruct state for resume: %s", exc)

    # Phase 7: narrow the visible toolbox if a scope was provided. When
    # ``tool_scope`` is None we keep the legacy module-level TOOL_DESCRIPTIONS
    # so behavior is unchanged for callers that don't pass a scope.
    if tool_scope:
        try:
            from tool_registry import build_react_tool_descriptions
            scoped_descriptions = build_react_tool_descriptions(allowed_tools=tool_scope)
        except Exception as exc:
            logger.warning("scoped tool descriptions failed; using full set: %s", exc)
            scoped_descriptions = TOOL_DESCRIPTIONS
    else:
        scoped_descriptions = TOOL_DESCRIPTIONS

    system = (
        REACT_SYSTEM
        .replace("{max_iterations}", str(max_iterations))
        .replace("{tool_descriptions}", scoped_descriptions)
    )

    # Build initial context from chat history
    history_context = ""
    if history:
        recent = history[-4:]
        history_context = "\n".join(
            f"{getattr(m, 'role', 'user')}: {getattr(m, 'content', str(m))[:200]}"
            for m in recent
        )
        history_context = f"\nRecent conversation:\n{history_context}\n"

    # Phase 2.2: prepend per-user conversation memory
    if memory_preamble:
        history_context = f"\n{memory_preamble}\n{history_context}"

    # Phase 1.4: prepend RAG-retrieved chunks
    if grounded_preamble:
        history_context = f"\n{grounded_preamble}\n{history_context}"

    for iteration in range(1, max_iterations + 1):
        if resume_iteration and iteration < resume_iteration:
            continue

        _end_iteration()
        iter_span = t.start_span("react_iteration")
        iter_span.set_attribute("iteration_number", iteration)
        iter_token = attach(otel_trace.set_span_in_context(iter_span))
        if iter_tracker is not None:
            iter_tracker["span"] = iter_span
            iter_tracker["token"] = iter_token

        if is_cancelled and is_cancelled():
            logger.info("react_loop: cancellation requested, aborting")
            ans = "[Investigation cancelled by user]"
            ans = _sanitize_user_facing_answer(ans)
            res_obj = ReActResult(
                answer=ans,
                tool_used=last_tool,
                result=last_result,
                steps=steps,
                total_iterations=iteration - 1,
                total_duration_ms=(time.perf_counter() - loop_start) * 1000,
                error="Cancelled",
            )
            res_obj.synthesis_breakdown = _build_result_synthesis_breakdown(ans, steps=steps, result=res_obj.result)
            _rec_fail(
                run_recorder,
                error="Cancelled",
                status="aborted",
                final_answer=ans,
                final_tool=last_tool,
                total_tokens_in=usage_tracker.total.tokens_in,
                total_tokens_out=usage_tracker.total.tokens_out,
                total_cached_tokens_in=usage_tracker.total.cached_tokens_in,
                total_cost_usd=usage_tracker.total.cost_usd,
            )
            return _record_metrics_and_return(res_obj, run_recorder)

        # Wall-clock timeout — don't let the loop run forever
        elapsed = time.perf_counter() - loop_start
        if time.perf_counter() >= effective_deadline:
            logger.warning("react_wall_clock_timeout elapsed=%.1fs", elapsed)
            deterministic_answer = _deterministic_investigate_pod_answer(primary_result)
            if deterministic_answer:
                ans = deterministic_answer
            else:
                # The deadline is already exhausted. Do not start another LLM
                # request; return deterministic evidence gathered so far.
                ans = _emergency_answer(steps, question)
                ans = _sanitize_user_facing_answer(ans)
                _emit({"type": "answer_end", "iteration": iteration - 1, "fallback_used": True})
            res_obj = ReActResult(
                answer=ans,
                tool_used=primary_tool or last_tool,
                result=primary_result or last_result,
                steps=steps,
                total_iterations=iteration - 1,
                total_duration_ms=elapsed * 1000,
                error=f"Investigation timed out after {int(elapsed)}s",
                suggested_actions=_extract_actions_from_steps(
                    steps,
                    primary_result or last_result,
                    answer_text=ans,
                    reviewer_provider=provider,
                    question=question,
                    usage_tracker=usage_tracker,
                ),
            )
            res_obj.synthesis_breakdown = _build_result_synthesis_breakdown(ans, steps=steps, result=res_obj.result)
            _rec_fail(
                run_recorder,
                error=f"Investigation timed out after {int(elapsed)}s",
                status="aborted",
                final_answer=ans,
                final_tool=primary_tool or last_tool,
                total_tokens_in=usage_tracker.total.tokens_in,
                total_tokens_out=usage_tracker.total.tokens_out,
                total_cached_tokens_in=usage_tracker.total.cached_tokens_in,
                total_cost_usd=usage_tracker.total.cost_usd,
            )
            return _record_metrics_and_return(res_obj, run_recorder)

        step_start = time.perf_counter()
        if resume_iteration and iteration == resume_iteration:
            thought = "Executing the user-approved action."
            action = resume_action
            params = resume_params
            _emit({
                "type": "iteration_planned",
                "iteration": iteration,
                "thought": thought,
                "action": action,
                "params": params,
            })
            if resume_step_id:
                try:
                    db.approve_agent_step(resume_run_id, resume_step_id, approver_user_id=approver_user_id)
                except Exception as exc:
                    logger.warning("Failed to approve pending step in DB: %s", exc)
        else:
            # Build the prompt with accumulated observations
            if context_mgr:
                try:
                    observations = context_mgr.budget_check_and_compact(
                        observations=envelope_observations,
                        user_message=question,
                        iteration=iteration
                    )
                except Exception as exc:
                    logger.warning("budget_check_and_compact failed: %s", exc)
                    observations = [json.dumps(env) for env in envelope_observations]
            else:
                observations = [json.dumps(env) for env in envelope_observations]

            prompt = _build_prompt(question, history_context, observations, iteration, max_iterations)

            # Ask the LLM what to do next
            step_budget = 8000
            if time.perf_counter() >= effective_deadline:
                continue
            try:
                _iter = iteration  # captured for closure below
                raw, think_usage = _stream_with_usage(
                    provider, prompt,
                    system=system, temperature=0.1, max_tokens=step_budget,
                    on_chunk=lambda c: _emit({"type": "thought_stream", "iteration": _iter, "text": c}),
                )
                usage_tracker.add(think_usage)
            except Exception as e:
                logger.warning(f"ReAct LLM call failed at iteration {iteration}: {e}")
                ans = _emergency_answer(steps, question)
                ans = _sanitize_user_facing_answer(ans)
                res_obj = ReActResult(
                    answer=ans,
                    tool_used=last_tool,
                    result=last_result,
                    steps=steps,
                    total_iterations=iteration,
                    total_duration_ms=(time.perf_counter() - loop_start) * 1000,
                    error=f"LLM error at step {iteration}: {e}",
                )
                res_obj.synthesis_breakdown = _build_result_synthesis_breakdown(ans, steps=steps, result=res_obj.result)
                _rec_fail(
                    run_recorder,
                    error=f"LLM error at step {iteration}: {e}",
                    final_answer=ans,
                    final_tool=last_tool,
                    total_tokens_in=usage_tracker.total.tokens_in,
                    total_tokens_out=usage_tracker.total.tokens_out,
                    total_cached_tokens_in=usage_tracker.total.cached_tokens_in,
                    total_cost_usd=usage_tracker.total.cost_usd,
                )
                return _record_metrics_and_return(res_obj, run_recorder)

            # Parse the LLM's response
            parsed = _parse_react_response(raw)
            if parsed is None:
                logger.warning(f"ReAct: unparseable response at iteration {iteration}: {raw[:200]}")
                # Try one more time with a nudge
                if iteration < max_iterations:
                    nudge = (
                        f"[System: Your last response was not valid JSON. "
                        f"Respond with ONLY a JSON object with 'thought', 'action', and 'params' or 'answer'.]"
                    )
                    if context_mgr:
                        try:
                            envelope = context_mgr.wrap_observation_envelope("system_nudge", {}, nudge)
                        except Exception as exc:
                            logger.warning("wrap_observation_envelope failed: %s", exc)
                            envelope = {"tool": "system_nudge", "observation": nudge, "trust": "system"}
                    else:
                        envelope = {"tool": "system_nudge", "observation": nudge, "trust": "system"}
                    envelope_observations.append(envelope)
                    continue
                else:
                    ans = _emergency_answer(steps, question)
                    res_obj = ReActResult(
                        answer=ans,
                        tool_used=last_tool,
                        result=last_result,
                        steps=steps,
                        total_iterations=iteration,
                        total_duration_ms=(time.perf_counter() - loop_start) * 1000,
                        error="Failed to parse LLM response",
                    )
                    res_obj.synthesis_breakdown = _build_result_synthesis_breakdown(ans, steps=steps, result=res_obj.result)
                    _rec_fail(
                        run_recorder,
                        error="Failed to parse LLM response",
                        final_answer=ans,
                        final_tool=last_tool,
                        total_tokens_in=usage_tracker.total.tokens_in,
                        total_tokens_out=usage_tracker.total.tokens_out,
                        total_cached_tokens_in=usage_tracker.total.cached_tokens_in,
                        total_cost_usd=usage_tracker.total.cost_usd,
                    )
                    return _record_metrics_and_return(res_obj, run_recorder)

            thought = parsed.get("thought", "")
            action = parsed.get("action", "")
            params = parsed.get("params", {})
            action, params, thought = _coerce_action_for_question(
                question,
                action,
                params,
                thought,
                has_primary_investigation=_has_useful_pod_investigation(primary_result),
                has_attempted_primary_investigation=primary_tool == "investigate_pod",
            )
            action, params, thought = _coerce_after_pod_discovery(
                question,
                action,
                params,
                thought,
                discovered_failing_pod=discovered_failing_pod,
                has_primary_investigation=_has_useful_pod_investigation(primary_result),
            )

            # Emit "iteration_planned" so UI can show "Calling <action>..."
            _emit({
                "type": "iteration_planned",
                "iteration": iteration,
                "thought": thought,
                "action": action,
                "params": params,
            })

        # ── Final answer ─────────────────────────────────────────────────
        if action == "answer":
            fallback_answer = parsed.get("answer", "") or ""

            _emit({"type": "answer_start", "iteration": iteration})
            streamed_text = ""
            stream_error: Optional[str] = None
            deterministic_answer = _deterministic_investigate_pod_answer(primary_result)
            if deterministic_answer:
                streamed_text = deterministic_answer
                _emit({"type": "token", "text": deterministic_answer})
            else:
                # Deduplicate envelopes
                envs_to_dedup = [(s.envelope, s.iteration) for s in steps if s.envelope is not None]
                deduped_envs = _dedupe_envelopes(envs_to_dedup)

                # Build causality chain
                causality_chain = _build_causality_chain(steps)

                # Dynamic system prompt with schema injection
                finalize_system = _build_finalize_system(deduped_envs)
                
                try:
                    streamed_text = stream_finalize_with_critic(
                        provider=provider,
                        question=question,
                        history_context=history_context,
                        envelopes=deduped_envs,
                        causality_chain=causality_chain,
                        finalize_system=finalize_system,
                        budget_exhausted=False,
                        on_event=_emit,
                        is_cancelled=is_cancelled,
                        retrieval_context=grounded_preamble,
                        usage_tracker=usage_tracker,
                    )
                except Exception as exc:  # provider error, network blip, etc.
                    stream_error = str(exc)
                    logger.warning("Streaming finalize failed: %s; using draft", exc)

            answer_text = _sanitize_user_facing_answer(streamed_text or fallback_answer)
            _emit({
                "type": "answer_end",
                "iteration": iteration,
                "fallback_used": bool(stream_error) and not streamed_text,
            })

            step = ReActStep(
                iteration=iteration,
                thought=thought,
                action="answer",
                duration_ms=(time.perf_counter() - step_start) * 1000,
            )
            steps.append(step)

            logger.info(
                "react_complete iterations=%d tools_called=%d streamed=%s",
                iteration,
                len([s for s in steps if s.action != "answer"]),
                bool(streamed_text),
            )

            res_obj = ReActResult(
                answer=answer_text,
                tool_used=primary_tool or last_tool,
                result=primary_result or last_result,
                steps=steps,
                total_iterations=iteration,
                total_duration_ms=(time.perf_counter() - loop_start) * 1000,
                suggested_actions=_extract_actions_from_steps(
                    steps,
                    primary_result or last_result,
                    answer_text=answer_text,
                    reviewer_provider=provider,
                    question=question,
                    usage_tracker=usage_tracker,
                ),
            )
            res_obj.synthesis_breakdown = _build_result_synthesis_breakdown(answer_text, steps=steps, result=res_obj.result)
            _answer_step_usage = usage_tracker.take_step()
            _rec_step(run_recorder,
                      iteration=iteration,
                      action="answer",
                      status="ok" if not stream_error else "error",
                      step_kind="answer",
                      thought=thought,
                      observation_preview=answer_text,
                      error_message=stream_error,
                      tokens_in=_answer_step_usage.tokens_in,
                      cached_tokens_in=_answer_step_usage.cached_tokens_in,
                      tokens_out=_answer_step_usage.tokens_out,
                      cost_usd=_answer_step_usage.cost_usd,
                      step_model=_answer_step_usage.model or getattr(provider, "model", None),
                      duration_ms=round(step.duration_ms))
            _rec_finish(run_recorder,
                        final_answer=answer_text,
                        final_tool=primary_tool or last_tool,
                        total_tokens_in=usage_tracker.total.tokens_in,
                        total_tokens_out=usage_tracker.total.tokens_out,
                        total_cached_tokens_in=usage_tracker.total.cached_tokens_in,
                        total_cost_usd=usage_tracker.total.cost_usd)
            return _record_metrics_and_return(res_obj, run_recorder)

        # ── Tool call ────────────────────────────────────────────────────
        logger.info(
            "react_step iteration=%d action=%s params=%s",
            iteration, action, json.dumps(params)[:100],
        )

        is_duplicate = False
        call_key: Optional[tuple] = None
        if action and action != "answer":
            try:
                serialized_params = json.dumps(params, sort_keys=True)
            except Exception:
                serialized_params = str(params)
            call_key = (action, serialized_params)
            if call_key in executed_calls:
                is_duplicate = True

        tool_start = time.perf_counter()
        envelope_obj = None
        try:
            if is_duplicate:
                result = {
                    "error": "duplicate_tool_call",
                    "message": (
                        f"You already called tool '{action}' with these exact parameters. "
                        "Repeating the same query will not yield new information. "
                        "If you have enough info, use action: 'answer' to formulate your response. "
                        "Otherwise, choose a different tool or vary the parameters."
                    )
                }
            elif tool_scope and action and action != "answer" and action not in tool_scope:
                # Phase 7: out-of-scope tool. Surface a recovery hint listing
                # the allowed tools so the agent can try again with a valid choice.
                # Deliberately do NOT add to executed_calls — the call never ran,
                # so a retry hits this same scope error rather than a misleading
                # 'duplicate_tool_call' message.
                _allowed_sorted = sorted(tool_scope)
                result = {
                    "error": "tool_out_of_scope",
                    "message": (
                        f"Tool '{action}' isn't available for this task. "
                        f"Pick one of: {', '.join(_allowed_sorted)}."
                    ),
                }
            else:
                # Mark as executed only when we actually dispatch — keeps duplicate
                # detection honest when other guards (scope) blocked a prior attempt.
                if call_key is not None:
                    executed_calls.add(call_key)

                is_mutating = action in {"delete_pod", "rollout_restart", "scale_deployment", "apply_patch"}
                is_confirming = params.get("confirm") or (approved_token is not None)
                if is_mutating and not is_confirming:
                    params["dry_run"] = True
                    if "confirm" in params:
                        del params["confirm"]

                # Retry logic for transient/rate-limited errors
                max_retries = 2
                retry_count = 0
                backoff_sec = 0.5
                while True:
                    if time.perf_counter() >= effective_deadline:
                        result = {
                            "error": "deadline_exceeded",
                            "message": "Investigation deadline exceeded before tool execution",
                        }
                        error_code = "deadline_exceeded"
                        error_msg = result["message"]
                        break
                    try:
                        dispatch_start = time.perf_counter()
                        result = dispatch_fn(action, params)
                        dispatch_duration = time.perf_counter() - dispatch_start
                        _record_tool_dispatch(action, dispatch_duration, result)

                        # Check for ToolEnvelope object
                        try:
                            from services.tool_envelope import ToolEnvelope
                            if isinstance(result, ToolEnvelope):
                                envelope_obj = result
                                result = result.model_dump(by_alias=True)
                        except ImportError:
                            pass
                        if action == "investigate_pod" and isinstance(result, dict):
                            result = _with_root_cause_summary(result)

                        error_code = result.get("error") if isinstance(result, dict) else None
                        error_msg = result.get("message") if isinstance(result, dict) else ""
                    except Exception as e:
                        result = {"error": "unexpected", "message": str(e)}
                        error_code = "unexpected"
                        error_msg = str(e)

                    if error_code:
                        from agent_errors import classify_error, AgentErrorType
                        err_type = classify_error(str(error_code), str(error_msg))
                        if err_type == AgentErrorType.TRANSIENT and retry_count < max_retries:
                            if time.perf_counter() + backoff_sec >= effective_deadline:
                                break
                            retry_count += 1
                            logger.info(
                                "react_loop: Transient error '%s' encountered on tool '%s'. Retrying (%d/%d) in %.1fs...",
                                error_code, action, retry_count, max_retries, backoff_sec
                            )
                            time.sleep(backoff_sec)
                            backoff_sec *= 2.0
                            continue
                        elif err_type == AgentErrorType.RATE_LIMITED and retry_count < 1:
                            if time.perf_counter() + 1.5 >= effective_deadline:
                                break
                            retry_count += 1
                            logger.warning(
                                "react_loop: Rate limit '%s' encountered on tool '%s'. Backing off 1.5s...",
                                error_code, action
                            )
                            time.sleep(1.5)
                            continue
                    break

                # If it was a dry_run and returned a confirmation token, suspend the run!
                if is_mutating and not is_confirming:
                    if isinstance(result, dict) and result.get("confirmation_token"):
                        token = result["confirmation_token"]
                        preview = result.get("preview") or ""

                        # Mark run suspended
                        run_id = run_recorder.run_id if run_recorder else str(uuid.uuid4())
                        db.suspend_agent_run(run_id)

                        # Record pending step
                        obs_id = str(uuid.uuid4())
                        step_id = _rec_step(
                            run_recorder,
                            iteration=iteration,
                            action=action,
                            status="pending_approval",
                            step_kind="tool",
                            thought=thought,
                            params=params,
                            observation_preview=preview,
                            observation_ref=obs_id,
                            duration_ms=round((time.perf_counter() - step_start) * 1000)
                        )

                        # Save raw observation
                        try:
                            source, trust_level = "system_discovery", "system"
                            if context_mgr:
                                source, trust_level = context_mgr.get_tool_metadata(action)
                            db.save_agent_observation(
                                id=obs_id,
                                run_id=run_id,
                                step_id=step_id,
                                tool=action,
                                source=source,
                                trust_level=trust_level,
                                content_type="application/json",
                                content=json.dumps(result),
                                summary=None,
                                redaction_status="redacted",
                                bytes_in=len(json.dumps(result)),
                                bytes_out=len(json.dumps(result)),
                            )
                        except Exception as exc:
                            logger.warning("save_agent_observation for pending step failed: %s", exc)

                        # Emit approval_required event
                        _emit({
                            "type": "approval_required",
                            "run_id": run_id,
                            "step_id": step_id,
                            "dry_run_preview": preview,
                            "confirmation_token": token,
                            "action": action,
                            "params": params,
                        })

                        # Add step to in-memory list
                        step_obj = ReActStep(
                            iteration=iteration,
                            thought=thought,
                            action=action,
                            action_params=params,
                            observation=preview,
                            duration_ms=(time.perf_counter() - step_start) * 1000,
                            envelope=envelope_obj,
                        )
                        steps.append(step_obj)

                        # Return ReActResult indicating suspension
                        ans = f"[Operation {action} requires human approval]"
                        res_obj = ReActResult(
                            answer=ans,
                            tool_used=action,
                            result=result,
                            steps=steps,
                            total_iterations=iteration,
                            total_duration_ms=(time.perf_counter() - loop_start) * 1000,
                            error="PendingApproval",
                        )
                        return _record_metrics_and_return(res_obj, run_recorder)
                
                # Verification Loop for mutating operations (Phase 4)
                if is_mutating and is_confirming:
                    error_code = result.get("error") if isinstance(result, dict) else None
                    if not error_code:
                        try:
                            parent_run_id = run_recorder.run_id if run_recorder else resume_run_id
                            if parent_run_id:
                                report = run_verification_sub_run(
                                    parent_run_id=parent_run_id,
                                    action=action,
                                    params=params,
                                    dispatch_fn=dispatch_fn,
                                    provider=provider,
                                    parent_recorder=run_recorder,
                                    context_mgr=context_mgr,
                                )
                                if isinstance(result, dict):
                                    result["verification_report"] = report
                                    result["message"] = f"Action executed. Verification result:\n{report}"
                        except Exception as exc:
                            logger.warning("Failed to run verification loop: %s", exc)

                last_tool = action
                last_result = result
                if _should_replace_primary_result(primary_tool, action):
                    primary_tool = action
                    primary_result = result
                candidate = _discover_failing_pod_from_result(question, action, result)
                if candidate:
                    discovered_failing_pod = candidate
        except Exception as e:
            result = {"error": str(e)}
            logger.warning(f"ReAct tool dispatch failed: {action} → {e}")

        tool_ms = (time.perf_counter() - tool_start) * 1000

        # Truncate observation to keep context manageable
        obs_text = _truncate_observation(result, action)

        step = ReActStep(
            iteration=iteration,
            thought=thought,
            action=action,
            action_params=params,
            observation=obs_text,
            duration_ms=(time.perf_counter() - step_start) * 1000,
            envelope=envelope_obj,
        )
        steps.append(step)

        # Emit "step_complete" so UI can mark the pill done + show duration.
        _emit({
            "type": "step_complete",
            "iteration": iteration,
            "action": action,
            "duration_ms": round(step.duration_ms),
            "preview": obs_text[:200],
        })

        error_code = result.get("error") if isinstance(result, dict) else None
        error_msg = result.get("message") if isinstance(result, dict) else ""
        if error_code:
            from agent_errors import classify_error
            _rec_err_type = classify_error(str(error_code), str(error_msg)).value
            _rec_err_msg = str(error_msg) if error_msg else str(error_code)
        else:
            _rec_err_type = None
            _rec_err_msg = None

        obs_id = str(uuid.uuid4())
        _step_usage = usage_tracker.take_step()
        step_id = _rec_step(
            run_recorder,
            iteration=iteration,
            action=action,
            status="error" if error_code else "ok",
            step_kind="tool",
            thought=thought,
            params=params,
            observation_preview=obs_text,
            observation_ref=obs_id,
            error_type=_rec_err_type,
            error_message=_rec_err_msg,
            tokens_in=_step_usage.tokens_in,
            cached_tokens_in=_step_usage.cached_tokens_in,
            tokens_out=_step_usage.tokens_out,
            cost_usd=_step_usage.cost_usd,
            step_model=_step_usage.model or getattr(provider, "model", None),
            duration_ms=round(step.duration_ms)
        )

        # 1. Convert result to string and redact it
        raw_result_str = ""
        if isinstance(result, dict):
            try:
                raw_result_str = json.dumps(result, default=str)
            except Exception:
                raw_result_str = str(result)
        else:
            raw_result_str = str(result)

        try:
            if context_mgr:
                redacted_content = context_mgr.redact_observation(raw_result_str)
            else:
                from services.rag.redaction import redact
                redacted_content = redact(raw_result_str)
        except Exception as exc:
            logger.warning("Redaction failed: %s; using original raw_result_str", exc)
            redacted_content = raw_result_str

        # 2. Save full raw output to agent_observations
        try:
            if context_mgr:
                source, trust_level = context_mgr.get_tool_metadata(action)
            else:
                source, trust_level = "system_discovery", "system"

            db.save_agent_observation(
                id=obs_id,
                run_id=context_mgr.run_id if context_mgr else str(uuid.uuid4()),
                step_id=step_id,
                tool=action,
                source=source,
                trust_level=trust_level,
                content_type="application/json",
                content=redacted_content,
                summary=None,
                redaction_status="redacted",
                bytes_in=len(raw_result_str),
                bytes_out=len(redacted_content),
            )
        except Exception as exc:
            logger.warning("save_agent_observation failed: %s", exc)

        # 3. Generate recovery directive if needed
        obs_to_wrap = redacted_content
        recovery_applied = False
        if error_code in ("unknown_tool", "tool_unavailable", "invalid_params", "tool_out_of_scope"):
            recovery_applied = True
            if error_code == "invalid_params":
                recovery = (
                    "[System: The previous tool call had invalid parameters. "
                    "Use the expected_schema/details in the observation and call the same tool "
                    "again with corrected params, or choose a better valid tool.]"
                )
            elif error_code == "tool_out_of_scope":
                _allowed = sorted(tool_scope) if tool_scope else _valid_react_tools()
                recovery = (
                    "[System: The previous tool isn't available for this task. "
                    f"Choose exactly one of these in-scope tools next: {', '.join(_allowed)}.]"
                )
            else:
                recovery = (
                    "[System: The previous action was not a callable tool. "
                    f"Choose exactly one of these valid tools next: {', '.join(_valid_react_tools())}.]"
                )
            obs_to_wrap = f"{redacted_content}\n{recovery}"

        # 4. Wrap and add to envelope_observations
        if context_mgr:
            try:
                envelope = context_mgr.wrap_observation_envelope(action, params, obs_to_wrap)
            except Exception as exc:
                logger.warning("wrap_observation_envelope failed: %s", exc)
                envelope = {"tool": action, "observation": obs_to_wrap, "trust": "untrusted"}
        else:
            envelope = {"tool": action, "observation": obs_to_wrap, "trust": "untrusted"}
        envelope_observations.append(envelope)

        if recovery_applied:
            continue

        if (
            iteration == max_iterations
            and action == "get_pods"
            and discovered_failing_pod
            and not _has_useful_pod_investigation(primary_result)
        ):
            forced_params = {
                "namespace": discovered_failing_pod["namespace"],
                "pod_name": discovered_failing_pod["name"],
                "use_ai": True,
            }
            try:
                forced_key = ("investigate_pod", json.dumps(forced_params, sort_keys=True))
            except Exception:
                forced_key = ("investigate_pod", str(forced_params))
            if forced_key not in executed_calls:
                executed_calls.add(forced_key)
                forced_thought = (
                    "Deterministic follow-up: final inventory found a concrete failing pod, "
                    "so investigate it before synthesizing a root-cause answer."
                )
                _emit({
                    "type": "iteration_planned",
                    "iteration": iteration + 1,
                    "thought": forced_thought,
                    "action": "investigate_pod",
                    "params": forced_params,
                })
                forced_start = time.perf_counter()
                forced_envelope = None
                try:
                    dispatch_start = time.perf_counter()
                    forced_result = dispatch_fn("investigate_pod", forced_params)
                    dispatch_duration = time.perf_counter() - dispatch_start
                    _record_tool_dispatch("investigate_pod", dispatch_duration, forced_result)
                    try:
                        from services.tool_envelope import ToolEnvelope
                        if isinstance(forced_result, ToolEnvelope):
                            forced_envelope = forced_result
                            forced_result = forced_result.model_dump(by_alias=True)
                    except ImportError:
                        pass
                    if isinstance(forced_result, dict):
                        forced_result = _with_root_cause_summary(forced_result)
                    last_tool = "investigate_pod"
                    last_result = forced_result
                    primary_tool = "investigate_pod"
                    primary_result = forced_result
                except Exception as exc:
                    forced_result = {"error": str(exc)}
                    logger.warning("Forced pod investigation failed: %s", exc)

                forced_obs = _truncate_observation(forced_result, "investigate_pod")
                forced_step = ReActStep(
                    iteration=iteration + 1,
                    thought=forced_thought,
                    action="investigate_pod",
                    action_params=forced_params,
                    observation=forced_obs,
                    duration_ms=(time.perf_counter() - forced_start) * 1000,
                    envelope=forced_envelope,
                )
                steps.append(forced_step)
                _emit({
                    "type": "step_complete",
                    "iteration": iteration + 1,
                    "action": "investigate_pod",
                    "duration_ms": round(forced_step.duration_ms),
                    "preview": forced_obs[:200],
                })


    # Exhausted iterations — synthesize best-effort answer
    logger.warning("react_max_iterations_reached iterations=%d", max_iterations)
    deterministic_answer = _deterministic_investigate_pod_answer(primary_result)
    if deterministic_answer:
        ans = deterministic_answer
    else:
        _emit({"type": "answer_start", "iteration": max_iterations})
        envs_to_dedup = [(s.envelope, s.iteration) for s in steps if s.envelope is not None]
        deduped_envs = _dedupe_envelopes(envs_to_dedup)
        causality_chain = _build_causality_chain(steps)
        finalize_system = _build_finalize_system(deduped_envs)
        try:
            ans = stream_finalize_with_critic(
                provider=provider,
                question=question,
                history_context=history_context,
                envelopes=deduped_envs,
                causality_chain=causality_chain,
                finalize_system=finalize_system,
                budget_exhausted=True,
                on_event=_emit,
                is_cancelled=is_cancelled,
                retrieval_context=grounded_preamble,
                usage_tracker=usage_tracker,
            )
        except Exception as exc:
            logger.warning("Max iterations finalize failed: %s; using emergency answer", exc)
            ans = _emergency_answer(steps, question)
        ans = _sanitize_user_facing_answer(ans)
        _emit({"type": "answer_end", "iteration": max_iterations, "fallback_used": False})
    res_obj = ReActResult(
        answer=ans,
        tool_used=primary_tool or last_tool,
        result=primary_result or last_result,
        steps=steps,
        total_iterations=max_iterations,
        total_duration_ms=(time.perf_counter() - loop_start) * 1000,
        error=None if deterministic_answer else "Reached maximum investigation steps",
        suggested_actions=_extract_actions_from_steps(
            steps,
            primary_result or last_result,
            answer_text=ans,
            reviewer_provider=provider,
            question=question,
            usage_tracker=usage_tracker,
        ),
    )
    res_obj.synthesis_breakdown = _build_result_synthesis_breakdown(ans, steps=steps, result=res_obj.result)
    if deterministic_answer:
        _rec_finish(run_recorder, final_answer=ans, final_tool=primary_tool or last_tool,
                    total_tokens_in=usage_tracker.total.tokens_in,
                    total_tokens_out=usage_tracker.total.tokens_out,
                    total_cached_tokens_in=usage_tracker.total.cached_tokens_in,
                    total_cost_usd=usage_tracker.total.cost_usd)
    else:
        _rec_fail(
            run_recorder,
            error="Reached maximum investigation steps",
            final_answer=ans,
            final_tool=primary_tool or last_tool,
            total_tokens_in=usage_tracker.total.tokens_in,
            total_tokens_out=usage_tracker.total.tokens_out,
            total_cached_tokens_in=usage_tracker.total.cached_tokens_in,
            total_cost_usd=usage_tracker.total.cost_usd,
        )
    _end_iteration()
    return _record_metrics_and_return(res_obj, run_recorder)


# ── Prompt construction ──────────────────────────────────────────────────────

HEADING_PATTERNS = {
    "diagnosis": re.compile(
        r"^\s*#{1,6}\s+diagnosis\s*:?\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    "evidence": re.compile(
        r"^\s*#{1,6}\s+evidence\s*:?\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    "recommended_actions": re.compile(
        r"^\s*#{1,6}\s+(?:recommended\s+actions?|next\s+steps?)\s*:?\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    "uncertainty": re.compile(
        r"^\s*#{1,6}\s+(?:uncertainty|confidence|caveats?)\s*:?\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
}

CONFIDENCE_PATTERN = re.compile(
    r"confidence\s*[:\-]\s*(low|medium|high)",
    re.IGNORECASE,
)


def _dedupe_envelopes(envelopes_with_steps: list) -> list:
    """Deduplicate envelopes, keeping the latest and setting revised_from_step if changed."""
    groups = {}
    for env, step_idx in envelopes_with_steps:
        if env is None:
            continue
        tool = env.meta.tool if hasattr(env, "meta") else env.get("_meta", {}).get("tool", "")
        params = env.meta.params if hasattr(env, "meta") else env.get("_meta", {}).get("params", {})
        params = params or {}
        cleaned_params = {k: v for k, v in params.items() if v is not None and v != ""}
        cleaned_params = {k: v for k, v in cleaned_params.items() if k != "use_ai"}
        params_key = json.dumps(cleaned_params, sort_keys=True)
        key = (tool, params_key)
        if key not in groups:
            groups[key] = []
        groups[key].append((env, step_idx))

    deduped = []
    for key, group in groups.items():
        latest_env, latest_step = group[-1]
        
        earlier_diff_step = None
        for earlier_env, earlier_step in group[:-1]:
            v_earlier = earlier_env.verdict if hasattr(earlier_env, "verdict") else earlier_env.get("verdict")
            v_latest = latest_env.verdict if hasattr(latest_env, "verdict") else latest_env.get("verdict")
            verdict_diff = v_earlier != v_latest
            
            earlier_ev = earlier_env.evidence if hasattr(earlier_env, "evidence") else earlier_env.get("evidence") if isinstance(earlier_env, dict) else getattr(earlier_env, "evidence", None)
            latest_ev = latest_env.evidence if hasattr(latest_env, "evidence") else latest_env.get("evidence") if isinstance(latest_env, dict) else getattr(latest_env, "evidence", None)
            if hasattr(earlier_ev, "model_dump"):
                earlier_ev = earlier_ev.model_dump()
            if hasattr(latest_ev, "model_dump"):
                latest_ev = latest_ev.model_dump()
            evidence_diff = earlier_ev != latest_ev
            
            if verdict_diff or evidence_diff:
                earlier_diff_step = earlier_step
                break
        
        if earlier_diff_step is not None:
            # Deep-copy before mutating so we don't corrupt the original
            # envelope still referenced by ReActStep.envelope. ``revised_from_step``
            # is recorded on ``meta`` (added to ToolMeta in tool_envelope.py),
            # NOT on ``params`` — params semantically holds only what the
            # tool was actually called with.
            if hasattr(latest_env, "model_copy"):
                latest_env = latest_env.model_copy(deep=True)
                if latest_env.meta is not None:
                    latest_env.meta.revised_from_step = earlier_diff_step
            elif isinstance(latest_env, dict):
                latest_env = json.loads(json.dumps(latest_env))
                if "_meta" not in latest_env:
                    latest_env["_meta"] = {}
                latest_env["_meta"]["revised_from_step"] = earlier_diff_step

        deduped.append(latest_env)

    return deduped


def _build_causality_chain(steps: list[ReActStep]) -> list[dict]:
    chain = []
    tool_steps = [s for s in steps if s.action != "answer"]
    for i, step in enumerate(tool_steps):
        if i == 0:
            trigger = "user_query"
            trigger_path = None
            trigger_reason = None
        else:
            deterministic = _deterministic_causality_trigger(step, tool_steps[:i])
            if deterministic:
                trigger = f"step_{deterministic['step']}"
                trigger_path = deterministic.get("trigger_path")
                trigger_reason = deterministic.get("reason")
            else:
                trigger_path = None
                trigger_reason = None
                match = re.search(r"step\s+(\d+)", step.thought, re.IGNORECASE)
                if match:
                    ref_step_num = int(match.group(1))
                    if 1 <= ref_step_num <= len(steps):
                        trigger = f"step_{ref_step_num}"
                    else:
                        trigger = "agent_chose_next"
                else:
                    trigger = "agent_chose_next"

        entry = {
            "step": step.iteration,
            "tool": step.action,
            "params": step.action_params,
            "trigger": trigger,
        }
        if trigger_path:
            entry["trigger_path"] = trigger_path
        if trigger_reason:
            entry["trigger_reason"] = trigger_reason

        chain.append(entry)
    return chain


def _deterministic_causality_trigger(step: ReActStep, previous_steps: list[ReActStep]) -> Optional[dict]:
    """Link a follow-up tool to the prior evidence that made it relevant."""
    if step.action == "investigate_pod":
        return _causality_from_pod_inventory(step, previous_steps)
    if step.action in {"get_service", "get_endpoints"}:
        return _causality_from_service_dependency(step, previous_steps)
    if step.action == "investigate_node":
        return _causality_from_event_node_issue(step, previous_steps)
    return None


def _causality_from_pod_inventory(step: ReActStep, previous_steps: list[ReActStep]) -> Optional[dict]:
    pod_name = _first_present(step.action_params, "pod_name", "name", "pod")
    namespace = _first_present(step.action_params, "namespace", "ns")
    if not pod_name:
        return None

    for prior in reversed(previous_steps):
        if prior.action != "get_pods":
            continue
        evidence = _step_evidence(prior)
        items, path_base = _pod_inventory_items(evidence)
        if not isinstance(items, list):
            continue
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            candidate_name = _first_present(item, "name", "pod_name", "pod")
            candidate_ns = _first_present(item, "namespace", "ns") or _first_present(prior.action_params, "namespace", "ns")
            if candidate_name == pod_name and (not namespace or not candidate_ns or candidate_ns == namespace):
                return {
                    "step": prior.iteration,
                    "trigger_path": f"{path_base}[{idx}]",
                    "reason": f"get_pods discovered pod {pod_name}",
                }
    return None


def _causality_from_service_dependency(step: ReActStep, previous_steps: list[ReActStep]) -> Optional[dict]:
    service_name = _first_present(step.action_params, "service_name", "service", "name")
    namespace = _first_present(step.action_params, "namespace", "ns")
    if not service_name:
        return None

    for prior in reversed(previous_steps):
        if prior.action != "investigate_pod":
            continue
        evidence = _step_evidence(prior)
        factors, path_base = _service_dependency_factors(evidence)
        if not isinstance(factors, list):
            continue
        for idx, factor in enumerate(factors):
            if not isinstance(factor, dict):
                continue
            factor_service = _first_present(factor, "service", "service_name", "target")
            factor_ns = _first_present(factor, "namespace", "ns") or _first_present(prior.action_params, "namespace", "ns")
            if not _service_names_match(service_name, factor_service):
                continue
            if namespace and factor_ns and factor_ns != namespace:
                continue
            return {
                "step": prior.iteration,
                "trigger_path": f"{path_base}[{idx}]",
                "reason": f"investigate_pod found service dependency {service_name}",
            }
    return None


def _causality_from_event_node_issue(step: ReActStep, previous_steps: list[ReActStep]) -> Optional[dict]:
    node_name = _first_present(step.action_params, "node_name", "node", "name")
    if not node_name:
        return None

    for prior in reversed(previous_steps):
        if prior.action != "get_events":
            continue
        evidence = _step_evidence(prior)
        if not isinstance(evidence, dict):
            continue
        recent = evidence.get("most_recent_critical")
        if isinstance(recent, dict) and _event_mentions_node(recent, node_name):
            return {
                "step": prior.iteration,
                "trigger_path": "evidence.most_recent_critical",
                "reason": f"get_events found node issue for {node_name}",
            }
        if isinstance(evidence.get("top_messages"), list) and evidence.get("top_messages"):
            messages = evidence.get("top_messages") or []
            path_base = "evidence.top_messages"
        else:
            messages = evidence.get("events") or []
            path_base = "events"
        if not isinstance(messages, list):
            continue
        for idx, message in enumerate(messages):
            if isinstance(message, dict) and _event_mentions_node(message, node_name):
                return {
                    "step": prior.iteration,
                    "trigger_path": f"{path_base}[{idx}]",
                    "reason": f"get_events found node issue for {node_name}",
                }
    return None


def _pod_inventory_items(evidence: dict) -> tuple[Optional[list], str]:
    if not isinstance(evidence, dict):
        return None, ""
    if isinstance(evidence.get("items"), list):
        return evidence["items"], "evidence.items"
    if isinstance(evidence.get("pods"), list):
        return evidence["pods"], "pods"
    return None, ""


def _service_dependency_factors(evidence: dict) -> tuple[Optional[list], str]:
    if not isinstance(evidence, dict):
        return None, ""
    if isinstance(evidence.get("contributing_factors"), list):
        return evidence["contributing_factors"], "evidence.contributing_factors"
    summary = evidence.get("evidence_summary")
    if isinstance(summary, dict) and isinstance(summary.get("dependency_checks"), list):
        return summary["dependency_checks"], "evidence_summary.dependency_checks"
    if isinstance(evidence.get("dependency_checks"), list):
        return evidence["dependency_checks"], "dependency_checks"
    return None, ""


def _step_evidence(step: ReActStep) -> dict:
    if step.envelope is not None:
        env = _as_plain_dict(step.envelope)
        evidence = env.get("evidence") if isinstance(env, dict) else None
        return evidence if isinstance(evidence, dict) else {}

    parsed = _json_object_from_text(step.observation)
    evidence = parsed.get("evidence") if isinstance(parsed, dict) else None
    return evidence if isinstance(evidence, dict) else parsed if isinstance(parsed, dict) else {}


def _json_object_from_text(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}


def _first_present(data: dict, *keys: str) -> str:
    if not isinstance(data, dict):
        return ""
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return str(value)
    return ""


def _service_names_match(requested: str, candidate: str) -> bool:
    if not requested or not candidate:
        return False
    requested = _normalize_service_reference(requested)
    candidate = _normalize_service_reference(candidate)
    return requested == candidate


def _normalize_service_reference(value: str) -> str:
    value = (value or "").strip()
    value = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", value)
    value = value.split("/", 1)[0]
    value = value.split(":", 1)[0]
    return value.split(".", 1)[0]


def _event_mentions_node(event: dict, node_name: str) -> bool:
    involved = event.get("involved_object") or event.get("involvedObject") or {}
    if isinstance(involved, dict):
        kind = str(involved.get("kind") or "").lower()
        name = str(involved.get("name") or "")
        if kind == "node" and name == node_name:
            return True
    object_ref = str(event.get("object") or "")
    if object_ref == f"Node/{node_name}":
        return True
    message = f"{event.get('reason', '')} {event.get('message', '')}"
    return re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(node_name)}(?![A-Za-z0-9_.-])", message) is not None


def build_envelope_retrieval_context(steps: list[ReActStep]) -> list[str]:
    """Serialize deduped envelope evidence for eval Faithfulness context.

    This intentionally includes structured ``evidence`` fields only. It excludes
    ``raw_excerpt`` so evals measure whether the final answer is grounded in the
    parsed evidence contract, not in raw observation text.
    """
    envs_to_dedup = [(s.envelope, s.iteration) for s in steps if s.envelope is not None]
    contexts: list[str] = []
    for idx, env in enumerate(_dedupe_envelopes(envs_to_dedup)):
        evidence = None
        if hasattr(env, "evidence"):
            evidence = env.evidence
        elif isinstance(env, dict):
            evidence = env.get("evidence")

        if evidence is None:
            continue
        if hasattr(evidence, "model_dump"):
            evidence = evidence.model_dump()

        try:
            evidence_text = json.dumps(evidence, indent=2, sort_keys=True, default=str)
        except Exception:
            evidence_text = str(evidence)
        if evidence_text.strip():
            contexts.append(f"ToolEnvelope evidence {idx}:\n{evidence_text}")
    return contexts


_EVIDENCE_PRIORITY_RANKS = {
    "verified_root_cause": 100,
    "primary_failure": 80,
    "dependency_check": 70,
    "container_log_finding": 60,
    "secondary_issue": 40,
    "ai_advisory": 20,
}

_EVIDENCE_PRIORITY_LABELS = {
    "verified_root_cause": "Verified root cause",
    "primary_failure": "Primary failure",
    "dependency_check": "Dependency check",
    "container_log_finding": "Container log finding",
    "secondary_issue": "Secondary issue",
    "ai_advisory": "AI advisory",
}


def build_synthesis_breakdown(
    text: str,
    *,
    steps: Optional[list[ReActStep]] = None,
    envelopes: Optional[list] = None,
) -> dict:
    """Parse Markdown and attach deterministic evidence-priority metadata."""
    breakdown = parse_markdown_synthesis(text)
    if envelopes is None and steps is not None:
        envs_to_dedup = [(s.envelope, s.iteration) for s in steps if s.envelope is not None]
        envelopes = _dedupe_envelopes(envs_to_dedup)
    priority = build_evidence_priority_summary(envelopes or [])
    if priority.get("primary_root_cause"):
        breakdown["evidence_priority"] = priority
    return breakdown


def _build_result_synthesis_breakdown(
    text: str,
    *,
    steps: Optional[list[ReActStep]] = None,
    result: Optional[dict] = None,
) -> dict:
    breakdown = build_synthesis_breakdown(text, steps=steps)
    root_summary = result.get("root_cause_summary") if isinstance(result, dict) else None
    if isinstance(root_summary, dict):
        breakdown["root_cause_summary"] = root_summary
    return breakdown


def build_evidence_priority_summary(envelopes: list) -> dict:
    """Return deterministic priority selection for audit/eval visibility."""
    candidates = _collect_evidence_priority_candidates(envelopes)
    if not candidates:
        return {"primary_root_cause": None, "candidates": []}
    candidates.sort(key=lambda item: (-item["priority_rank"], item["envelope_index"], item["evidence_path"]))
    return {
        "primary_root_cause": candidates[0],
        "candidates": candidates[:5],
    }


def _collect_evidence_priority_candidates(envelopes: list) -> list[dict]:
    candidates: list[dict] = []
    for env_idx, env in enumerate(envelopes or []):
        env_dict = _as_plain_dict(env)
        evidence = env_dict.get("evidence") if isinstance(env_dict, dict) else None
        if not isinstance(evidence, dict):
            continue

        source_tool = _nested_get(env_dict, ["_meta", "tool"]) or _nested_get(env_dict, ["meta", "tool"])
        target = (
            evidence.get("primary_target")
            or evidence.get("target")
            or evidence.get("source")
            or {}
        )

        for field_name in ("failure_modes", "contributing_factors", "timeline", "top_messages", "drift"):
            values = evidence.get(field_name) or []
            if not isinstance(values, list):
                continue
            for item_idx, item in enumerate(values):
                if not isinstance(item, dict):
                    continue
                candidate = _priority_candidate_from_item(
                    item,
                    envelope_index=env_idx,
                    evidence_path=f"evidence.{field_name}[{item_idx}]",
                    source_tool=source_tool,
                    target=target,
                )
                if candidate:
                    candidates.append(candidate)

        recent = evidence.get("most_recent_critical")
        if isinstance(recent, dict):
            candidate = _priority_candidate_from_item(
                recent,
                envelope_index=env_idx,
                evidence_path="evidence.most_recent_critical",
                source_tool=source_tool,
                target=target,
            )
            if candidate:
                candidates.append(candidate)

    return candidates


def _priority_candidate_from_item(
    item: dict,
    *,
    envelope_index: int,
    evidence_path: str,
    source_tool: Optional[str],
    target: dict,
) -> Optional[dict]:
    priority = str(item.get("evidence_priority") or _infer_evidence_priority(item))
    if not priority:
        return None
    rank_int = _normalized_priority_rank(item, priority)

    summary = _summarize_priority_item(item)
    searchable = json.dumps(item, default=str).lower()
    if priority == "primary_failure" and ("oomkilled" in searchable or "out of memory" in searchable):
        rank_int = max(rank_int, 90)
        label = "Primary failure: OOMKilled"
    else:
        label = str(item.get("priority_label") or _EVIDENCE_PRIORITY_LABELS.get(priority, priority))

    return {
        "priority": priority,
        "priority_rank": rank_int,
        "label": label,
        "summary": summary,
        "severity": item.get("severity"),
        "source_tool": source_tool,
        "envelope_index": envelope_index,
        "evidence_path": evidence_path,
        "target": target if isinstance(target, dict) else {},
    }


def _infer_evidence_priority(item: dict) -> str:
    if item.get("type") == "verified_root_cause" or item.get("root_cause"):
        return "verified_root_cause"
    if item.get("service_exists") is False or item.get("ready_count") == 0:
        return "dependency_check"
    if item.get("ai"):
        return "ai_advisory"
    if item.get("container") and (item.get("excerpt") or item.get("reason") or item.get("restart_count")):
        return "container_log_finding"
    if item.get("mode") or item.get("condition") or item.get("warning_events"):
        return "primary_failure"
    if item.get("findings"):
        return "ai_advisory"
    if item.get("reason") or item.get("message"):
        return "secondary_issue"
    return ""


def _normalized_priority_rank(item: dict, priority: str) -> int:
    """Normalize ranks so priority class, not producer noise, determines order."""
    base_rank = _EVIDENCE_PRIORITY_RANKS.get(priority, 0)
    if priority != "ai_advisory":
        return base_rank

    try:
        requested_rank = int(item.get("priority_rank"))
    except Exception:
        requested_rank = base_rank

    if requested_rank <= base_rank:
        return base_rank

    if not _ai_advisory_override_allowed(item):
        return base_rank

    # Advisory evidence may override primary/dependency evidence only when it is
    # explicitly high-confidence and justified; verified deterministic root
    # cause still remains the strongest evidence class.
    return min(requested_rank, _EVIDENCE_PRIORITY_RANKS["verified_root_cause"] - 1)


def _ai_advisory_override_allowed(item: dict) -> bool:
    justification = str(
        item.get("priority_justification")
        or item.get("justification")
        or item.get("rationale")
        or ""
    ).strip()
    confidence = str(
        item.get("confidence")
        or item.get("confidence_band")
        or item.get("confidence_level")
        or ""
    ).strip().lower().replace("_", " ")
    return bool(justification) and confidence in {"high", "very high", "verified", "strong"}


def _summarize_priority_item(item: dict) -> str:
    if item.get("root_cause"):
        return str(item.get("root_cause"))
    service = item.get("service") or item.get("service_name")
    namespace = item.get("namespace")
    if item.get("service_exists") is False and service:
        suffix = f" in namespace `{namespace}`" if namespace else ""
        return f"Service `{service}` does not exist{suffix}."
    if item.get("mode"):
        container = item.get("container")
        return f"{container + ' ' if container else ''}container is in {item.get('mode')}."
    if item.get("container"):
        reason = item.get("reason") or item.get("message") or item.get("excerpt")
        return f"{item.get('container')}: {reason}" if reason else str(item.get("container"))
    if item.get("reason") or item.get("message"):
        reason = item.get("reason")
        message = item.get("message")
        return f"{reason}: {message}" if reason and message else str(reason or message)
    if item.get("condition"):
        return f"Node condition: {item.get('condition')}"
    compact = json.dumps(item, default=str, sort_keys=True)
    return compact[:300]


def _as_plain_dict(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True)
    if hasattr(value, "dict"):
        return value.dict()
    return value


def _nested_get(data: dict, path: list[str]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _build_finalize_system(envelopes: list) -> str:
    used_schemas = {}
    try:
        from services.tool_envelope import (
            InventoryEvidence,
            DiagnosticEvidence,
            StatusCheckEvidence,
            LogAnalysisEvidence
        )
        evidence_classes = {
            "inventory": InventoryEvidence,
            "diagnostic": DiagnosticEvidence,
            "status_check": StatusCheckEvidence,
            "log_analysis": LogAnalysisEvidence,
        }
        
        for env in envelopes:
            evidence = env.evidence if hasattr(env, "evidence") else env.get("evidence") if isinstance(env, dict) else getattr(env, "evidence", None)
            if not evidence:
                continue
            
            evidence_type = None
            if hasattr(evidence, "type"):
                evidence_type = evidence.type
            elif isinstance(evidence, dict):
                evidence_type = evidence.get("type")
                
            if evidence_type in evidence_classes:
                cls = evidence_classes[evidence_type]
                if cls.__name__ not in used_schemas:
                    try:
                        used_schemas[cls.__name__] = cls.model_json_schema()
                    except Exception as e:
                        logger.warning("Could not generate schema for %s: %s", cls.__name__, e)
    except Exception as e:
        logger.warning("Error resolving schemas for system prompt: %s", e)

    schemas_str = ""
    if used_schemas:
        schemas_str = json.dumps(used_schemas, indent=2)
    else:
        schemas_str = "(No evidence schemas injected)"

    system_prompt = f"""You are a senior Kubernetes/DevOps engineer. Write a clear, helpful answer to the user's question using the investigation findings.
You MUST format your response in plain Markdown. Do NOT respond with JSON or wrap the entire response in a JSON block or code fences.

Your response MUST contain these exact top-level headings in this exact order:

# Diagnosis
2-4 sentences naming the verdict and the root cause. Required. Lead with the verdict and cause supported by the highest-priority evidence item. If any diagnostic failure_mode is marked as a verified root cause, use that as the primary root cause. Do not introduce claims that don't appear in an envelope or in retrieval_context.

# Evidence
Bulleted evidence in user-readable language. Each bullet MUST be grounded in a specific ToolEnvelope evidence field, but DO NOT print internal field paths or bracketed envelope references. Mention the concrete resource name, namespace, status, event, log message, or config value instead. Required.
Follow evidence_priority metadata when present. Priority order is: verified_root_cause, primary_failure, dependency_check, container_log_finding, secondary_issue, ai_advisory. The highest-priority evidence item should drive the headline diagnosis unless another deterministic item has a higher priority_rank. Do not present secondary container issues or advisory AI analysis as the primary root cause when a verified or primary deterministic cause is present.

# Recommended Actions
Ordered list of concrete next steps. Each step must reference a specific resource by name and namespace where applicable. Mark any destructive step with "(requires approval)". May be empty for inventory queries — in that case say "No actions required; this is an inventory result." Required header.
If you have enough concrete configuration data to propose a safe executable fix, include either a whitelisted kubectl write command in backticks or a `# patch:apply` YAML block. Do not invent missing manifests, service selectors, image names, or replacement values.

# Uncertainty
Plain English. First line must be exactly: "Confidence: low | medium | high". Then prose explaining what reduced confidence (e.g., envelope-level data_completeness signals) and what would resolve it. Required.

If you want to propose a configuration fix (like a pod or deployment update), generate the exact Kubernetes YAML and wrap it in a markdown block (```yaml). Make the very first line `# patch:apply` so the user can easily apply it.

If you cannot produce a section, write "(no <section name> applicable)" under the heading. Do NOT omit headings.

--- Injected Schemas for Evidence ---
{schemas_str}
"""
    return system_prompt


def compute_confidence_band(envelopes: list, budget_exhausted: bool) -> str:
    """Determine confidence from evidence relevance, completeness, and conflict."""
    return compute_confidence_report(envelopes, budget_exhausted)["band"]


def compute_confidence_report(envelopes: list, budget_exhausted: bool) -> dict:
    """Return confidence band plus reasons suitable for the Uncertainty prompt."""
    if budget_exhausted:
        return {"band": "low", "reasons": ["investigation budget was exhausted"]}
    if not envelopes:
        return {"band": "low", "reasons": ["no tool evidence was gathered"]}

    reasons: list[str] = []
    completeness_values = []
    for env in envelopes:
        completeness_values.append(_confidence_data_completeness(env))

    if "stale" in completeness_values:
        reasons.append("at least one evidence envelope is stale")
    if "partial" in completeness_values:
        reasons.append("at least one evidence envelope is partial")

    evidence_types = [_envelope_evidence_type(env) for env in envelopes]
    non_inventory_types = [t for t in evidence_types if t and t != "inventory"]
    if not non_inventory_types:
        reasons.append("available evidence is inventory-only and does not establish a root cause")

    contradiction_reason = _confidence_contradiction_reason(envelopes)
    if contradiction_reason:
        reasons.append(contradiction_reason)

    if reasons:
        return {"band": "low", "reasons": reasons}

    priority = build_evidence_priority_summary(envelopes)
    candidates = priority.get("candidates") or []
    has_verified_root = any(c.get("priority") == "verified_root_cause" for c in candidates)
    has_primary_failure = any(c.get("priority") == "primary_failure" for c in candidates)
    has_corrob = any(
        c.get("priority") in {"primary_failure", "dependency_check", "container_log_finding"}
        for c in candidates
    )

    if has_verified_root and has_corrob:
        return {
            "band": "high",
            "reasons": ["verified root cause is corroborated by diagnostic, dependency, or log evidence"],
        }
    if has_verified_root:
        return {
            "band": "medium",
            "reasons": ["verified root cause is present but lacks independent corroborating evidence"],
        }
    if has_primary_failure:
        return {
            "band": "medium",
            "reasons": ["clear failure mode is present but direct root cause is not verified"],
        }

    return {
        "band": "low",
        "reasons": ["non-inventory evidence does not identify a clear failure mode or verified cause"],
    }


def _confidence_data_completeness(env: Any) -> str:
    env_dict = _as_plain_dict(env)
    sig = None
    if isinstance(env_dict, dict):
        sig = env_dict.get("confidence_signals")
    if sig is None and hasattr(env, "confidence_signals"):
        sig = env.confidence_signals

    if hasattr(sig, "data_completeness"):
        return str(sig.data_completeness or "complete")
    if isinstance(sig, dict):
        return str(sig.get("data_completeness", "complete"))
    return "complete"


def _envelope_evidence_type(env: Any) -> str:
    env_dict = _as_plain_dict(env)
    evidence = env_dict.get("evidence") if isinstance(env_dict, dict) else None
    if isinstance(evidence, dict):
        return str(evidence.get("type") or "")
    return ""


def _confidence_contradiction_reason(envelopes: list) -> str:
    diagnostic_verdicts_by_target: dict[str, set[str]] = {}
    verified_roots_by_target: dict[str, set[str]] = {}
    for env in envelopes or []:
        env_dict = _as_plain_dict(env)
        if not isinstance(env_dict, dict):
            continue
        evidence = env_dict.get("evidence")
        evidence_type = evidence.get("type") if isinstance(evidence, dict) else ""
        verdict = env_dict.get("verdict")
        target_key = _confidence_target_key(evidence) if isinstance(evidence, dict) else ""
        if evidence_type in {"diagnostic", "status_check", "log_analysis"} and verdict in {"Healthy", "Unhealthy"}:
            diagnostic_verdicts_by_target.setdefault(target_key, set()).add(verdict)
        if isinstance(evidence, dict):
            for item in evidence.get("failure_modes") or []:
                if isinstance(item, dict) and (item.get("type") == "verified_root_cause" or item.get("root_cause")):
                    verified_roots_by_target.setdefault(target_key, set()).add(
                        _normalize_confidence_text(str(item.get("root_cause") or item))
                    )

    for verdicts in diagnostic_verdicts_by_target.values():
        if {"Healthy", "Unhealthy"}.issubset(verdicts):
            return "evidence contains contradictory healthy and unhealthy diagnostic verdicts for the same target"
    for roots in verified_roots_by_target.values():
        if len(roots) > 1:
            return "multiple distinct verified root causes were found for the same target"
    return ""


def _confidence_target_key(evidence: dict) -> str:
    target = (
        evidence.get("primary_target")
        or evidence.get("target")
        or evidence.get("source")
        or {}
    )
    if not isinstance(target, dict):
        return "__unknown__"

    namespace = str(target.get("namespace") or target.get("ns") or "")
    for key in ("pod_name", "workload_name", "deployment_name", "service_name", "node_name", "name", "pod"):
        value = target.get(key)
        if value:
            return f"{namespace}/{key}:{value}"
    if namespace:
        return f"{namespace}/__namespace__"
    return "__unknown__"


def _normalize_confidence_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


_FINALIZE_SYSTEM = _build_finalize_system([])


def _build_finalize_prompt(
    question: str,
    history_context: str = "",
    *,
    envelopes: Optional[list] = None,
    causality_chain: Optional[list[dict]] = None,
    suggested_confidence: Optional[str] = None,
    confidence_reasons: Optional[list[str]] = None,
) -> str:
    """Build the prompt for the streaming final-answer call (Phase B).

    Only accepts structured envelopes (all tools are enveloped).
    """
    if envelopes is None:
        envelopes = []
    if causality_chain is None:
        causality_chain = []

    parts: list[str] = []
    if history_context:
        parts.append(history_context)

    parts.append(f"User question: {question}")

    if envelopes:
        priority_summary = build_evidence_priority_summary(envelopes)
        if priority_summary.get("primary_root_cause"):
            parts.append("\n--- Evidence Priority Summary ---")
            parts.append(json.dumps(priority_summary, indent=2, default=str))
            parts.append("--- End of Evidence Priority Summary ---")

        parts.append("\n--- Investigation findings (ToolEnvelopes) ---")
        for i, env in enumerate(envelopes):
            if hasattr(env, "model_dump"):
                env_dict = env.model_dump(by_alias=True)
            elif isinstance(env, dict):
                env_dict = env
            else:
                env_dict = str(env)
            parts.append(f"Envelope {i}:\n{json.dumps(env_dict, indent=2) if isinstance(env_dict, dict) else env_dict}")
        parts.append("--- End of findings ---")

    if not envelopes:
        parts.append(
            "(No tool output gathered. Answer the user from first "
            "principles, and suggest a kubectl command if it would help.)"
        )

    if causality_chain:
        parts.append("\n--- Causality Chain ---")
        parts.append(json.dumps(causality_chain, indent=2))
        parts.append("--- End of Causality Chain ---")

    if suggested_confidence:
        reason_text = ""
        if confidence_reasons:
            reason_text = " Calibration reasons: " + "; ".join(confidence_reasons) + "."
        parts.append(
            f"Based on the completeness and recency of the evidence, the deterministically calculated "
            f"suggested confidence is: {suggested_confidence}.\n"
            f"In your '# Uncertainty' section, you MUST output 'Confidence: {suggested_confidence}' "
            f"on the first line unless you have a strong reason to override it (in which case, justify "
            f"your override in the Uncertainty prose).{reason_text}"
        )

    parts.append(
        "Now write the final answer for the user. Follow the system prompt rules and structure."
    )
    return "\n\n".join(parts)


def stream_finalize_with_critic(
    provider: Any,
    question: str,
    history_context: str,
    envelopes: list,
    causality_chain: list,
    finalize_system: str,
    budget_exhausted: bool,
    on_event: Optional[Callable[[dict], None]],
    is_cancelled: Optional[Callable[[], bool]] = None,
    retrieval_context: str = "",
    usage_tracker: Optional[UsageTracker] = None,
) -> str:
    """Stream final answer with conditional critic checks and retries."""
    from services.synthesis_critic import run_synthesis_critic
    from services.llm.pricing import TokenUsage

    def _emit(event: dict) -> None:
        if on_event:
            try:
                on_event(event)
            except Exception as exc:
                logger.warning("Critic streaming event emit failed: %s", exc)

    # 1. Determine confidence
    confidence_report = compute_confidence_report(envelopes, budget_exhausted)
    confidence = confidence_report["band"]
    
    # 2. Check if we should pre-gate (only pre-gate low confidence if we have evidence envelopes)
    pre_gate = (confidence == "low" and len(envelopes) > 0) or budget_exhausted
    
    # Construct prompts
    finalize_prompt = _build_finalize_prompt(
        question,
        history_context,
        envelopes=envelopes,
        causality_chain=causality_chain,
        suggested_confidence=confidence,
        confidence_reasons=confidence_report.get("reasons") or [],
    )

    if pre_gate:
        # Pre-gating flow:
        # Show placeholder
        placeholder_text = "Verifying partial findings..." if budget_exhausted else "Analyzing findings..."
        _emit({"type": "placeholder", "text": placeholder_text})
        
        # Generate answer synchronously/buffered
        try:
            if hasattr(provider, "generate_with_usage"):
                answer, usage = provider.generate_with_usage(
                    finalize_prompt,
                    system=finalize_system,
                    temperature=0.2,
                    max_tokens=8000,
                )
            elif hasattr(provider, "generate"):
                answer = provider.generate(
                    finalize_prompt,
                    system=finalize_system,
                    temperature=0.2,
                    max_tokens=8000,
                )
                usage = TokenUsage.empty(model=getattr(provider, "model", ""))
            else:
                answer, usage = _stream_with_usage(
                    provider,
                    finalize_prompt,
                    system=finalize_system,
                    temperature=0.2,
                    max_tokens=8000,
                )
            if usage_tracker is not None:
                usage_tracker.add(usage)
        except Exception as exc:
            logger.warning("Synchronous finalize generation failed: %s", exc)
            answer = ""
            
        # Run critic
        critic_usage_holder = []
        critic_results = run_synthesis_critic(
            provider,
            question,
            envelopes,
            retrieval_context,
            answer,
            usage_holder=critic_usage_holder,
        )
        if usage_tracker is not None and critic_usage_holder:
            usage_tracker.add(critic_usage_holder[0])
        
        # Check if critic passed
        passed = all(check.get("passed", True) for check in critic_results.values())
        if not passed:
            logger.info("Critic rejected initial draft on pre-gated flow. Retrying once...")
            feedback_str = "\n".join(
                f"- Check '{k}' failed: {v.get('rationale')}" 
                for k, v in critic_results.items() if not v.get("passed", True)
            )
            retry_system = finalize_system + (
                f"\n\nCRITIC FEEDBACK ON PREVIOUS DRAFT:\n"
                f"The critic reviewed your previous draft and rejected it for the following reasons:\n"
                f"{feedback_str}\n"
                f"Please rewrite the entire answer, making sure to resolve all failed checks. "
                f"Keep the required markdown headings and structure."
            )
            try:
                if hasattr(provider, "generate_with_usage"):
                    answer, usage = provider.generate_with_usage(
                        finalize_prompt,
                        system=retry_system,
                        temperature=0.2,
                        max_tokens=8000,
                    )
                elif hasattr(provider, "generate"):
                    answer = provider.generate(
                        finalize_prompt,
                        system=retry_system,
                        temperature=0.2,
                        max_tokens=8000,
                    )
                    usage = TokenUsage.empty(model=getattr(provider, "model", ""))
                else:
                    answer, usage = _stream_with_usage(
                        provider,
                        finalize_prompt,
                        system=retry_system,
                        temperature=0.2,
                        max_tokens=8000,
                    )
                if usage_tracker is not None:
                    usage_tracker.add(usage)
            except Exception as exc:
                logger.warning("Synchronous finalize retry failed: %s", exc)

        answer = _sanitize_user_facing_answer(answer)
        # Now stream the final answer chunk by chunk (simulate streaming)
        chunk_size = 40
        for i in range(0, len(answer), chunk_size):
            if is_cancelled and is_cancelled():
                logger.info("react_loop: cancellation requested during pre-gated simulated stream, aborting")
                break
            chunk = answer[i:i+chunk_size]
            _emit({"type": "token", "text": chunk})
            
        return answer

    # Hybrid flow:
    # We stream tokens line-by-line until we hit the # Recommended Actions section.
    import re
    
    # Compile heading pattern
    rec_actions_pattern = re.compile(
        r"^\s*#{1,6}\s+(?:recommended\s+actions?|next\s+steps?)\s*:?\s*$",
        re.IGNORECASE | re.MULTILINE
    )
    
    streamed_text = ""
    buffered_rest = ""
    in_actions_section = False
    line_buffer = ""
    
    try:
        if hasattr(provider, "generate_stream_with_usage"):
            provider_stream, usage_holder = provider.generate_stream_with_usage(
                finalize_prompt,
                system=finalize_system,
                temperature=0.2,
                max_tokens=8000,
            )
        else:
            provider_stream = provider.generate_stream(
                finalize_prompt,
                system=finalize_system,
                temperature=0.2,
                max_tokens=8000,
            )
            usage_holder = []
        
        for chunk in provider_stream:
            if is_cancelled and is_cancelled():
                logger.info("react_loop: cancellation requested during finalize stream, aborting")
                break
            if not chunk:
                continue
                
            if in_actions_section:
                buffered_rest += chunk
            else:
                line_buffer += chunk
                while "\n" in line_buffer:
                    line, line_buffer = line_buffer.split("\n", 1)
                    if rec_actions_pattern.match(line):
                        in_actions_section = True
                        _emit({"type": "placeholder", "text": "Verifying recommendations..."})
                        buffered_rest = line + "\n" + line_buffer
                        line_buffer = ""
                        break
                    else:
                        sanitized_line = _sanitize_user_facing_answer(line + "\n")
                        _emit({"type": "token", "text": sanitized_line})
                        streamed_text += sanitized_line
                        
        # If stream finished and we are not in actions section:
        if not in_actions_section:
            if line_buffer:
                if rec_actions_pattern.match(line_buffer):
                    in_actions_section = True
                    _emit({"type": "placeholder", "text": "Verifying recommendations..."})
                    buffered_rest = line_buffer
                    line_buffer = ""
                else:
                    sanitized_line = _sanitize_user_facing_answer(line_buffer)
                    _emit({"type": "token", "text": sanitized_line})
                    streamed_text += sanitized_line
                    
        usage = usage_holder[0] if usage_holder else TokenUsage.empty(model=getattr(provider, "model", ""))
        if usage_tracker is not None:
            usage_tracker.add(usage)
    except Exception as exc:
        logger.warning("Streaming finalize failed during generation: %s", exc)
        return _sanitize_user_facing_answer(streamed_text + (buffered_rest if in_actions_section else line_buffer))

    # If we hit the actions section, we need to verify it
    if in_actions_section:
        if is_cancelled and is_cancelled():
            return streamed_text
            
        # Check if the buffered section is destructive
        actions_lower = buffered_rest.lower()
        
        # Build the destructive/requires-confirm tools list from the registry
        try:
            from tool_registry import TOOLS
            destructive_tools = [name.lower() for name, t in TOOLS.items() if t.write_op or t.requires_confirm]
        except Exception:
            destructive_tools = ["delete_pod", "rollout_restart", "scale_deployment", "apply_patch"]
            
        is_destructive = "(requires approval)" in actions_lower or any(tool in actions_lower for tool in destructive_tools)
        
        full_answer = streamed_text + buffered_rest
        
        if is_destructive:
            # Run critic on the full answer
            critic_usage_holder = []
            critic_results = run_synthesis_critic(
                provider,
                question,
                envelopes,
                retrieval_context,
                full_answer,
                usage_holder=critic_usage_holder,
            )
            if usage_tracker is not None and critic_usage_holder:
                usage_tracker.add(critic_usage_holder[0])
            
            passed = all(check.get("passed", True) for check in critic_results.values())
            if not passed:
                logger.info("Critic rejected destructive proposal. Retrying once...")
                feedback_str = "\n".join(
                    f"- Check '{k}' failed: {v.get('rationale')}" 
                    for k, v in critic_results.items() if not v.get("passed", True)
                )
                retry_system = finalize_system + (
                    f"\n\nCRITIC FEEDBACK ON PREVIOUS DRAFT:\n"
                    f"The critic reviewed your previous draft and rejected the Recommended Actions for the following reasons:\n"
                    f"{feedback_str}\n"
                    f"\nCRITICAL REQUIREMENT:\n"
                    f"You MUST preserve the '# Diagnosis' and '# Evidence' sections exactly as follows:\n"
                    f"{streamed_text.strip()}\n\n"
                    f"Only rewrite the '# Recommended Actions' and '# Uncertainty' sections to address the feedback."
                )
                try:
                    if hasattr(provider, "generate_with_usage"):
                        new_answer, usage = provider.generate_with_usage(
                            finalize_prompt,
                            system=retry_system,
                            temperature=0.2,
                            max_tokens=8000,
                        )
                    elif hasattr(provider, "generate"):
                        new_answer = provider.generate(
                            finalize_prompt,
                            system=retry_system,
                            temperature=0.2,
                            max_tokens=8000,
                        )
                        usage = TokenUsage.empty(model=getattr(provider, "model", ""))
                    else:
                        new_answer, usage = _stream_with_usage(
                            provider,
                            finalize_prompt,
                            system=retry_system,
                            temperature=0.2,
                            max_tokens=8000,
                        )
                    if usage_tracker is not None:
                        usage_tracker.add(usage)
                    
                    match = rec_actions_pattern.search(new_answer)
                    if match:
                        buffered_rest = new_answer[match.start():]
                    else:
                        buffered_rest = new_answer
                except Exception as exc:
                    logger.warning("Retry generation failed for destructive proposal: %s", exc)

        buffered_rest = _sanitize_user_facing_answer(buffered_rest)
        # Stream the remaining buffered_rest
        chunk_size = 40
        for i in range(0, len(buffered_rest), chunk_size):
            if is_cancelled and is_cancelled():
                logger.info("react_loop: cancellation requested during hybrid simulated stream, aborting")
                break
            chunk = buffered_rest[i:i+chunk_size]
            _emit({"type": "token", "text": chunk})
            
        return streamed_text + buffered_rest

    return streamed_text


def _sanitize_user_facing_answer(text: str) -> str:
    """Remove internal envelope field paths from user-facing streamed answers."""
    cleaned = re.sub(
        r"`?envelope\[(?:\d+|i)\](?:\.[A-Za-z_][A-Za-z0-9_]*|\[[^\]]+\])*`?\s*(?:[—–:-]\s*)?",
        "",
        text or "",
    )
    cleaned = re.sub(r"`?envelope\[(?:\d+|i)\]`?\s*", "", cleaned)
    cleaned = re.sub(r"(?m)^(\s*[-*]\s*)[—–-]\s*", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned


def parse_markdown_synthesis(text: str) -> dict:
    if not text:
        return {
            "diagnosis": "",
            "evidence_count": 0,
            "recommended_actions": [],
            "confidence_band": "unknown",
            "uncertainty_text": "",
            "parser_warnings": ["empty_input"]
        }

    warnings = []
    
    matches = []
    for section_key, pattern in HEADING_PATTERNS.items():
        for m in pattern.finditer(text):
            matches.append({
                "section": section_key,
                "start": m.start(),
                "end": m.end(),
                "matched_text": m.group(0),
            })
            
    matches.sort(key=lambda x: x["start"])
    
    expected_order = ["diagnosis", "evidence", "recommended_actions", "uncertainty"]
    actual_order = [m["section"] for m in matches]
    
    seen_sections = set()
    unique_actual_order = []
    for s in actual_order:
        if s not in seen_sections:
            seen_sections.add(s)
            unique_actual_order.append(s)
            
    order_idx = 0
    out_of_order = False
    for s in unique_actual_order:
        if s in expected_order:
            exp_pos = expected_order.index(s)
            if exp_pos < order_idx:
                out_of_order = True
            order_idx = exp_pos
            
    if out_of_order:
        warnings.append("headings_out_of_order")
        
    for key in expected_order:
        if key not in seen_sections:
            warnings.append(f"missing_heading:{key}")
            
    section_contents = {key: "" for key in expected_order}
    
    for i, match in enumerate(matches):
        sec_name = match["section"]
        
        matched_hdr = match["matched_text"].strip()
        hashes = len(matched_hdr) - len(matched_hdr.lstrip('#'))
        if hashes != 1:
            warnings.append(f"heading_level_drift:{sec_name}")
        if matched_hdr.endswith(':'):
            warnings.append(f"trailing_colon:{sec_name}")
            
        hdr_clean = matched_hdr.lstrip('#').strip().rstrip(':').strip().lower()
        if sec_name == "recommended_actions" and hdr_clean in ("recommended action", "next step", "next steps"):
            warnings.append("synonym_used:recommended_actions")
        elif sec_name == "uncertainty" and hdr_clean in ("confidence", "caveat", "caveats"):
            warnings.append("synonym_used:uncertainty")
            
        start_idx = match["end"]
        end_idx = matches[i + 1]["start"] if i + 1 < len(matches) else len(text)
        content = text[start_idx:end_idx].strip()
        
        if section_contents[sec_name]:
            section_contents[sec_name] += "\n\n" + content
        else:
            section_contents[sec_name] = content
            
    evidence_text = section_contents["evidence"]
    bullets = re.findall(r"^\s*[\*\-\+]\s+", evidence_text, re.MULTILINE)
    evidence_count = len(bullets)
    
    actions_text = section_contents["recommended_actions"]
    actions = []
    for line in actions_text.splitlines():
        line = line.strip()
        if not line:
            continue
        item_match = re.match(r"^(\d+\.|\*|\-|\+)\s+(.*)$", line)
        if item_match:
            actions.append(item_match.group(2).strip())
        elif line.lower() not in ("(no recommended actions applicable)", "no actions required; this is an inventory result."):
            actions.append(line)
            
    uncertainty_text = section_contents["uncertainty"]
    confidence_band = "unknown"
    confidence_match = CONFIDENCE_PATTERN.search(uncertainty_text)
    
    if confidence_match:
        confidence_band = confidence_match.group(1).lower()
        lines_clean = []
        for line in uncertainty_text.splitlines():
            if CONFIDENCE_PATTERN.search(line):
                continue
            lines_clean.append(line)
        uncertainty_text = "\n".join(lines_clean).strip()
    else:
        if "uncertainty" in seen_sections:
            warnings.append("confidence_missing")
            
    return {
        "diagnosis": section_contents["diagnosis"],
        "evidence_count": evidence_count,
        "recommended_actions": actions,
        "confidence_band": confidence_band,
        "uncertainty_text": uncertainty_text,
        "parser_warnings": warnings
    }


def _build_prompt(
    question: str,
    history_context: str,
    observations: list[str],
    iteration: int,
    max_iterations: int,
) -> str:
    """Build the prompt for the current ReAct iteration."""
    parts = []

    if history_context:
        parts.append(history_context)

    parts.append(f"User question: {question}")

    if observations:
        parts.append("\n--- Investigation so far ---")
        for obs in observations:
            parts.append(obs)
        parts.append("--- End of investigation so far ---\n")

        remaining = max_iterations - iteration
        if remaining <= 2:
            parts.append(
                f"[IMPORTANT: You have {remaining + 1} steps remaining. "
                f"Give your final answer NOW with what you have.]"
            )
        elif len(observations) >= 2:
            parts.append(
                "[You have already gathered data. If you can answer the question, "
                "do so now. Only call another tool if essential information is missing.]"
            )

    parts.append(
        f"Step {iteration}/{max_iterations}: What should you do next? "
        f"Respond with a JSON object."
    )

    return "\n\n".join(parts)


# ── Deterministic action guards ───────────────────────────────────────────────

def _coerce_action_for_question(
    question: str,
    action: str,
    params: dict,
    thought: str,
    *,
    has_primary_investigation: bool = False,
    has_attempted_primary_investigation: bool = False,
) -> tuple[str, dict, str]:
    """Correct high-risk tool choices where recent history can mislead the LLM.

    A common failure mode is a follow-up asking for "all node labels" after a
    prior node-specific resource question. The LLM may copy the old node_name
    into investigate_node even though the current prompt is cluster-wide.
    """
    q = (question or "").lower()
    asks_node_inventory = bool(re.search(r"\b(all|every|each)\b", q)) and bool(
        re.search(r"\bnodes?\b", q)
    )
    asks_node_labels = (asks_node_inventory or bool(re.search(r"\bnodes\b", q))) and bool(
        re.search(r"\blabels?\b", q)
    )
    asks_extra_node_fields = bool(
        re.search(r"\b(status|ready|roles?|capacity|allocatable|resources?|cpu|memory|version|os)\b", q)
    )
    labels_only = asks_node_labels and not asks_extra_node_fields
    node_focus = _node_focus_params_for_question(q)

    broad_node_focus = node_focus and (asks_node_inventory or bool(re.search(r"\bnodes\b", q)))

    pod_status = _pod_status_filter_for_question(q)
    pod_failure_target = _pod_failure_target_for_question(q)
    if pod_failure_target and pod_status and not has_primary_investigation and not has_attempted_primary_investigation:
        if action not in {"investigate_pod", "investigate_workload", "answer"}:
            corrected_thought = (
                f"{thought} Deterministic correction: the current question asks why "
                f"`{pod_failure_target}` pods are in {pod_status}; start from the failing "
                "pod investigation before checking dependencies."
            ).strip()
            corrected_params = {"pod_name": pod_failure_target, "use_ai": True}
            if isinstance(params, dict) and params.get("namespace"):
                corrected_params["namespace"] = params["namespace"]
            return "investigate_pod", corrected_params, corrected_thought
    if pod_status and _simple_pod_status_inventory_question(q):
        if action == "get_pods":
            corrected_params = dict(params or {})
            corrected_params.setdefault("namespace", "*")
            corrected_params["status_filter"] = pod_status
            return action, corrected_params, thought
        if action in ("investigate_pod", "get_pod_logs", "describe_pod", "list_namespace_resources", "get_events"):
            corrected_thought = (
                f"{thought} Deterministic correction: the current question asks whether pods "
                f"are in {pod_status} state, so use a filtered pod inventory and answer."
            ).strip()
            return "get_pods", {"namespace": "*", "status_filter": pod_status}, corrected_thought

    if action == "investigate_node" and (asks_node_inventory or asks_node_labels or broad_node_focus):
        corrected_thought = (
            f"{thought} Deterministic correction: the current question asks for "
            "cluster-wide node listing/focused node fields, so use get_nodes without node_name."
        ).strip()
        return "get_nodes", node_focus or ({"labels_only": True} if labels_only else {}), corrected_thought

    if action == "get_nodes" and (labels_only or node_focus):
        corrected_params = {k: v for k, v in (params or {}).items() if k != "node_name"}
        corrected_params.update(node_focus or {"labels_only": True})
        return action, corrected_params, thought

    pod_focus = _pod_focus_params_for_question(q)
    if pod_focus:
        asks_broad_pods = bool(re.search(r"\b(all|every|each|list|show|get|what|which)\b", q)) and bool(
            re.search(r"\bpods?\b", q)
        )
        if action == "get_pods":
            corrected_params = dict(params or {})
            corrected_params.update(pod_focus)
            if "namespace" not in corrected_params:
                corrected_params["namespace"] = "*"
            return action, corrected_params, thought
        if action in ("investigate_pod", "get_pod_logs", "describe_pod") and asks_broad_pods:
            corrected_thought = (
                f"{thought} Deterministic correction: the current question asks for "
                "a focused pod inventory, not a single-pod investigation."
            ).strip()
            corrected_params = {"namespace": "*", **pod_focus}
            return "get_pods", corrected_params, corrected_thought

    deployment_focus = _deployment_focus_params_for_question(q)
    if action == "get_deployment" and deployment_focus:
        corrected_params = dict(params or {})
        corrected_params.update(deployment_focus)
        return action, corrected_params, thought

    return action, params, thought


def _coerce_after_pod_discovery(
    question: str,
    action: str,
    params: dict,
    thought: str,
    *,
    discovered_failing_pod: Optional[dict],
    has_primary_investigation: bool,
) -> tuple[str, dict, str]:
    """After inventory discovers a concrete failing pod, investigate it before answering."""
    if has_primary_investigation or not discovered_failing_pod:
        return action, params, thought

    q = (question or "").lower()
    if not (_pod_failure_target_for_question(q) and _pod_status_filter_for_question(q)):
        return action, params, thought

    namespace = discovered_failing_pod.get("namespace")
    pod_name = discovered_failing_pod.get("name") or discovered_failing_pod.get("pod_name")
    if not namespace or not pod_name:
        return action, params, thought

    if action == "investigate_pod":
        corrected = dict(params or {})
        corrected.setdefault("namespace", namespace)
        corrected.setdefault("pod_name", pod_name)
        corrected["use_ai"] = True
        return action, corrected, thought

    corrected_thought = (
        f"{thought} Deterministic correction: inventory found failing pod "
        f"`{pod_name}` in namespace `{namespace}`; investigate that concrete pod before answering."
    ).strip()
    return (
        "investigate_pod",
        {"namespace": namespace, "pod_name": pod_name, "use_ai": True},
        corrected_thought,
    )


def _has_useful_pod_investigation(result: Optional[dict]) -> bool:
    """True when investigate_pod returned enough evidence for root-cause synthesis."""
    if not isinstance(result, dict):
        return False

    evidence_summary = result.get("evidence_summary")
    if isinstance(evidence_summary, dict) and (
        evidence_summary.get("suspected_root_cause")
        or evidence_summary.get("dependency_checks")
        or evidence_summary.get("secondary_issues")
        or evidence_summary.get("evidence")
    ):
        return True

    if result.get("container_log_findings"):
        return True

    classification = result.get("classification")
    if isinstance(classification, dict) and classification.get("mode") not in (None, "", "unknown"):
        return True

    evidence = result.get("evidence")
    if isinstance(evidence, dict):
        failure_modes = evidence.get("failure_modes") or []
        contributing = evidence.get("contributing_factors") or []
        if failure_modes or contributing:
            return True

    return False


def _discover_failing_pod_from_result(question: str, tool: str, result: Any) -> Optional[dict]:
    """Extract the first concrete failing pod matching a root-cause question."""
    if tool != "get_pods" or not isinstance(result, dict):
        return None

    q = (question or "").lower()
    target = _pod_failure_target_for_question(q)
    desired_status = _pod_status_filter_for_question(q)
    if not target or not desired_status:
        return None

    pods = result.get("pods")
    if not isinstance(pods, list):
        pods = result.get("items")
    if not isinstance(pods, list):
        return None

    target_norm = target.lower()
    desired_norm = desired_status.lower()
    candidates = []
    for pod in pods:
        if not isinstance(pod, dict):
            continue
        name = str(pod.get("name") or pod.get("pod_name") or "")
        namespace = str(pod.get("namespace") or result.get("namespace") or "")
        status = str(pod.get("status") or pod.get("phase") or pod.get("reason") or "")
        if not name or not namespace:
            continue
        name_norm = name.lower()
        if target_norm not in name_norm:
            continue
        if desired_norm and not _pod_status_matches_failure_request(status, desired_status):
            continue
        candidates.append({"namespace": namespace, "name": name, "status": status})

    if candidates:
        return candidates[0]
    return None


def _pod_status_matches_failure_request(status: str, desired_status: str) -> bool:
    status_norm = (status or "").lower()
    desired_norm = (desired_status or "").lower()
    if not desired_norm:
        return True
    if desired_norm in status_norm:
        return True
    if desired_norm == "crashloopbackoff":
        return status_norm not in ("", "running", "succeeded", "completed")
    return False


def _pod_failure_target_for_question(q: str) -> str:
    if not re.search(r"\b(why|identify|root cause|root-cause|debug|diagnose|investigate|troubleshoot|help me)\b", q):
        return ""
    if not re.search(r"\bpods?\b", q):
        return ""
    if not re.search(r"\b(crash|crashing|crashed|crashloop|crashlopp|crashloopbackoff|backoff|failing|failed|error|issue|broken)\b", q):
        return ""

    stopwords = {
        "all", "any", "the", "these", "those", "my", "our", "same",
        "which", "what", "why", "pods", "pod",
    }
    patterns = [
        r"\b([a-z0-9][a-z0-9_.-]*)\s+pods?\b",
        r"\bpods?\s+(?:named?|called)?\s*([a-z0-9][a-z0-9_.-]*)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, q)
        if not match:
            continue
        target = match.group(1).strip("-_.")
        if target and target not in stopwords:
            return target
    return ""


def _pod_status_filter_for_question(q: str) -> str:
    if re.search(r"crash\s*loop|crashloop|crashlopp|crashloopbackoff|crashing|crashed", q):
        return "CrashLoopBackOff"
    if re.search(r"imagepull|image\s*pull|errimagepull", q):
        return "ImagePullBackOff"
    if re.search(r"\bpending\b", q):
        return "Pending"
    if re.search(r"\boomkilled|oom\b", q):
        return "OOMKilled"
    if re.search(r"\bevicted\b", q):
        return "Evicted"
    return ""


def _simple_pod_status_inventory_question(q: str) -> bool:
    if not re.search(r"\bpods?\b", q):
        return False
    if re.search(
        r"\b("
        r"why|identify|root\s*cause|debug|diagnose|investigate|troubleshoot|help\s+me"
        r"|figure\s+out|what\s+should|what\s+do\s+i|how\s+do\s+i|determine"
        r"|check"
        r")\b",
        q,
    ):
        return False
    return bool(re.search(r"\b(any|are there|show|list|get|which|what)\b", q))


def _pod_focus_params_for_question(q: str) -> dict:
    if not re.search(r"\bpods?\b", q) and not re.search(r"\bimages?\b.*\brunning\b", q):
        return {}

    focus: dict[str, bool] = {}
    if re.search(r"\blabels?\b", q) and not re.search(r"\bstatus|ready|restart|image|resources?|requests?|limits?|cpu|memory|where|scheduled|node\b", q):
        focus["labels_only"] = True
    elif re.search(r"\bimages?\b", q):
        focus["images_only"] = True
    elif re.search(r"\b(resources?|requests?|limits?|cpu|memory)\b", q):
        focus["resources_only"] = True
    elif re.search(r"\b(where|placement|scheduled|node_selector|node selector|tolerations?|affinity|which nodes?)\b", q):
        focus["placement_only"] = True
    return focus


def _node_focus_params_for_question(q: str) -> dict:
    if not re.search(r"\bnodes?\b", q):
        return {}
    if re.search(r"\blabels?\b", q) and not re.search(r"\b(status|ready|roles?|capacity|allocatable|resources?|cpu|memory|version|os|taints?|conditions?|addresses?)\b", q):
        return {"labels_only": True}
    if re.search(r"\btaints?\b|unschedulable", q):
        return {"taints_only": True}
    if re.search(r"\bconditions?\b", q):
        return {"conditions_only": True}
    if re.search(r"\baddresses?|internalip|externalip|hostnames?\b", q):
        return {"addresses_only": True}
    return {}


def _deployment_focus_params_for_question(q: str) -> dict:
    if not re.search(r"\b(deployments?|deploy|rollout)\b", q):
        return {}

    focus: dict[str, bool] = {}
    if re.search(r"\blabels?\b", q) and not re.search(r"\bstatus|ready|replicas?|image|resources?|requests?|limits?|cpu|memory|template\b", q):
        focus["labels_only"] = True
    elif re.search(r"\bimages?\b", q):
        focus["images_only"] = True
    elif re.search(r"\b(resources?|requests?|limits?|cpu|memory)\b", q):
        focus["resources_only"] = True
    elif re.search(r"\b(template|pod template|service account|node selector|node_selector|tolerations?|affinity|volumes?)\b", q):
        focus["template_only"] = True
    return focus


# ── Response parsing ─────────────────────────────────────────────────────────

def _parse_react_response(raw: str) -> Optional[dict]:
    """Parse the LLM's JSON response, handling markdown code fences and truncation.

    The LLM sometimes returns truncated JSON (max_tokens hit mid-response).
    This parser tries progressively harder strategies to recover useful output:
    1. Direct JSON parse
    2. Extract JSON from surrounding text / code fences
    3. Salvage truncated "answer" responses by closing the string + object
    """
    if not raw:
        return None

    text = raw.strip()

    # Strip markdown code fences
    if "```" in text:
        for part in text.split("```"):
            part = part.strip().lstrip("json").strip()
            if part.startswith("{"):
                text = part
                break

    # Try direct JSON parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to extract JSON object from surrounding text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    # ── Salvage truncated / malformed responses ───────────────────────────

    # If the LLM dropped the opening {"thought": " prefix, prepend it and retry.
    # Pattern: text starts with the thought content directly, e.g.:
    #   I now have enough info",\n  "action": "answer", ...
    if start == -1 and '"action"' in text:
        repaired = '{"thought": "' + text
        end2 = repaired.rfind("}")
        if end2 > 0:
            try:
                return json.loads(repaired[:end2 + 1])
            except json.JSONDecodeError:
                pass

    # Work with the fragment from the first { (if any)
    fragment = text[start:] if start != -1 else text

    # Strategy: if it looks like an answer action, extract what we can
    answer_match = re.search(
        r'"action"\s*:\s*"answer".*?"answer"\s*:\s*"',
        fragment, re.DOTALL,
    )
    if answer_match:
        # Everything after the opening quote of "answer": " is the text
        answer_start = answer_match.end()
        # The answer text may be truncated — take what we have
        answer_body = fragment[answer_start:]
        # Strip trailing incomplete escape sequences or quotes
        answer_body = answer_body.rstrip("\\")
        if answer_body.endswith('"'):
            answer_body = answer_body[:-1]
        # Remove trailing } and whitespace
        answer_body = re.sub(r'"\s*\}\s*$', '', answer_body)
        # Unescape JSON string escapes
        try:
            answer_text = json.loads(f'"{answer_body}"')
        except json.JSONDecodeError:
            # Fallback: use raw text, replacing common escapes
            answer_text = answer_body.replace('\\"', '"').replace("\\n", "\n")

        # Also try to extract the thought
        thought_match = re.search(r'"thought"\s*:\s*"([^"]*)"', fragment)
        thought = thought_match.group(1) if thought_match else ""

        if answer_text.strip():
            return {
                "thought": thought,
                "action": "answer",
                "answer": answer_text.strip(),
            }

    # Strategy: if it looks like a tool call, try to extract action + params
    action_match = re.search(r'"action"\s*:\s*"([^"]+)"', fragment)
    params_match = re.search(r'"params"\s*:\s*\{([^}]*)\}', fragment)
    thought_match = re.search(r'"thought"\s*:\s*"([^"]*)"', fragment)
    if action_match and action_match.group(1) != "answer":
        action = action_match.group(1)
        thought = thought_match.group(1) if thought_match else ""
        params = {}
        if params_match:
            try:
                params = json.loads("{" + params_match.group(1) + "}")
            except json.JSONDecodeError:
                pass
        return {"thought": thought, "action": action, "params": params}

    # Ultimate fallback: if the response is not valid JSON and has no obvious tool call formatting,
    # treat the entire raw output as the final answer rather than crashing the system.
    if raw.strip():
        # If the output contains JSON-like structures but failed to parse,
        # return None to trigger the retry/nudge on earlier steps.
        if any(k in raw for k in ('"action"', '"thought"', '"params"')):
            return None
        return {
            "thought": "Model returned plain text instead of JSON; treating as final answer.",
            "action": "answer",
            "answer": raw.strip(),
        }

    return None


# ── Observation formatting ───────────────────────────────────────────────────

def _truncate_observation(result: dict, tool: str) -> str:
    """Convert a tool result dict to a string, truncated for context window.

    Every return path passes through ``sanitize_observation`` so secrets cannot
    leak into the next ReAct prompt or the persisted observation preview.
    """
    if isinstance(result, dict) and "verification_report" in result:
        return sanitize_observation(result["verification_report"], MAX_OBSERVATION_CHARS)

    if not isinstance(result, dict):
        return sanitize_observation(str(result), MAX_OBSERVATION_CHARS)

    # If the result represents an error, serialize it directly to preserve details
    if "error" in result:
        text = json.dumps(result, default=str)
    # For investigation tools, extract the most useful parts
    elif tool in ("investigate_pod", "investigate_workload", "analyze_namespace"):
        focused = {}
        for key in ("pod_name", "namespace", "classification", "steps_run",
                     "pod_spec_summary",
                     "workload_name", "workload_type", "workload_summary",
                     "related_pods_summary", "events_parsed", "summary", "issues", "issue_summary",
                     "pod_count", "health_summary", "evidence_summary", "container_log_findings", "error"):
            if key in result:
                focused[key] = result[key]
        # Include AI analysis if present
        ai = result.get("ai", {})
        if isinstance(ai, dict) and ai.get("ai_analysis"):
            focused["ai_analysis_advisory"] = ai["ai_analysis"]
        text = json.dumps(focused, default=str)
    elif tool == "investigate_node":
        allocated = result.get("allocated", {}) or {}
        capacity = result.get("capacity", {}) or {}
        allocatable = result.get("allocatable", {}) or {}
        focused = {
            "name": result.get("name") or result.get("query"),
            "query": result.get("query"),
            "status": result.get("status"),
            "roles": result.get("roles"),
            "capacity": {
                "cpu": capacity.get("cpu"),
                "cpu_millicores": capacity.get("cpu_millicores"),
                "memory_gib": capacity.get("memory_gib"),
            },
            "allocatable": {
                "cpu": allocatable.get("cpu"),
                "cpu_millicores": allocatable.get("cpu_millicores"),
                "memory_gib": allocatable.get("memory_gib"),
            },
            "allocated": {
                "cpu_requests_millicores": allocated.get("cpu_requests_millicores"),
                "cpu_requests_cores": allocated.get("cpu_requests_cores"),
                "cpu_requests_percent_of_allocatable": allocated.get("cpu_requests_percent_of_allocatable"),
                "cpu_limits_millicores": allocated.get("cpu_limits_millicores"),
                "cpu_limits_cores": allocated.get("cpu_limits_cores"),
                "cpu_limits_percent_of_allocatable": allocated.get("cpu_limits_percent_of_allocatable"),
                "memory_requests_gib": allocated.get("memory_requests_gib"),
                "memory_requests_percent_of_allocatable": allocated.get("memory_requests_percent_of_allocatable"),
                "memory_limits_gib": allocated.get("memory_limits_gib"),
                "memory_limits_percent_of_allocatable": allocated.get("memory_limits_percent_of_allocatable"),
                "non_terminated_pods": allocated.get("non_terminated_pods"),
            },
            "pods": result.get("pods", [])[:20] if isinstance(result.get("pods"), list) else [],
        }
        text = json.dumps(focused, default=str)
    elif tool == "get_pods":
        # Include health summary and full pod list so the LLM can answer listing questions
        focused = {
            "namespace": result.get("namespace"),
            "pod_count": result.get("pod_count"),
            "namespace_summary": result.get("namespace_summary"),
            "exclude_namespaces": result.get("exclude_namespaces"),
            "exclude_namespace_prefixes": result.get("exclude_namespace_prefixes"),
            "focused_modes": result.get("focused_modes"),
            "health_summary": result.get("health_summary"),
        }
        pods = result.get("pods", [])
        if pods:
            if result.get("focused_modes"):
                focused["pods"] = pods[:80]
            else:
                # Include all pod names/statuses (compact), with unhealthy first
                unhealthy = [p for p in pods if p.get("status") not in ("Running", "Succeeded")]
                healthy = [p for p in pods if p.get("status") in ("Running", "Succeeded")]
                ordered = unhealthy + healthy
                # Compact format: just name, status, restarts, age
                focused["pods"] = [
                    {k: p.get(k) for k in ("namespace", "name", "status", "restarts", "age", "ready") if p.get(k) is not None}
                    for p in ordered[:30]
                ]
            focused["total_pods"] = len(pods)
        text = json.dumps(focused, default=str)
    elif tool == "get_nodes":
        nodes = result.get("nodes", [])
        focused = {
            "node_count": result.get("node_count"),
            "nodes": [
                {
                    "name": node.get("name"),
                    "status": node.get("status"),
                    "roles": node.get("roles"),
                    "labels": node.get("labels", {}),
                    "label_count": node.get("label_count", len(node.get("labels", {}) or {})),
                    "annotations": node.get("annotations"),
                    "taints": node.get("taints"),
                    "unschedulable": node.get("unschedulable"),
                    "addresses": node.get("addresses"),
                    "conditions": node.get("conditions"),
                }
                for node in nodes
            ],
        }
        text = json.dumps(focused, default=str)
    elif tool == "get_deployment":
        if result.get("focused_modes"):
            text = json.dumps(result, default=str)
        else:
            focused = {
                "name": result.get("name"),
                "namespace": result.get("namespace"),
                "replicas": result.get("replicas"),
                "health_status": result.get("health_status"),
                "diagnostic_hint": result.get("diagnostic_hint"),
                "selector": result.get("selector"),
                "labels": result.get("labels", {}),
                "revision": result.get("revision"),
                "generation": result.get("generation"),
                "observed_generation": result.get("observed_generation"),
                "conditions": result.get("conditions", []),
                "pod_template": result.get("pod_template", {}),
            }
            text = json.dumps(focused, default=str)
    elif tool == "list_namespace_resources":
        focused = {
            "namespace": result.get("namespace"),
            "summary": result.get("summary"),
            "pods": result.get("pods", [])[:50],
            "services": result.get("services", [])[:50],
            "deployments": result.get("deployments", [])[:50],
            "statefulsets": result.get("statefulsets", [])[:50],
            "daemonsets": result.get("daemonsets", [])[:50],
            "configmaps": result.get("configmaps", [])[:50],
            "persistent_volume_claims": result.get("persistent_volume_claims", [])[:50],
            "ingresses": result.get("ingresses", [])[:50],
        }
        text = json.dumps(focused, default=str)
    elif tool == "get_endpoints":
        endpoint_slices = result.get("endpoint_slices", {}) or {}
        focused = {
            "name": result.get("name"),
            "namespace": result.get("namespace"),
            "has_endpoints": result.get("has_endpoints"),
            "ready_count": result.get("ready_count"),
            "not_ready_count": result.get("not_ready_count"),
            "ready_addresses": result.get("ready_addresses", [])[:50],
            "not_ready_addresses": result.get("not_ready_addresses", [])[:50],
            "ports": result.get("ports", []),
            "diagnostic_hint": result.get("diagnostic_hint"),
            "endpoint_slice_count": result.get("endpoint_slice_count"),
            "endpoint_slice_endpoint_count": result.get("endpoint_slice_endpoint_count"),
            "endpoint_slices": {
                "slice_count": endpoint_slices.get("slice_count"),
                "endpoint_count": endpoint_slices.get("endpoint_count"),
                "ready_count": endpoint_slices.get("ready_count"),
                "not_ready_count": endpoint_slices.get("not_ready_count"),
                "serving_count": endpoint_slices.get("serving_count"),
                "terminating_count": endpoint_slices.get("terminating_count"),
                "ports": endpoint_slices.get("ports", []),
                "endpoints": endpoint_slices.get("endpoints", [])[:80],
                "diagnostic_hint": endpoint_slices.get("diagnostic_hint"),
                "error": endpoint_slices.get("error"),
            },
        }
        text = json.dumps(focused, default=str)
    elif tool == "get_service":
        focused = {
            "name": result.get("name"),
            "namespace": result.get("namespace"),
            "type": result.get("type"),
            "focused_modes": result.get("focused_modes"),
            "labels": result.get("labels", {}),
            "annotations": result.get("annotations"),
            "cluster_ip": result.get("cluster_ip"),
            "cluster_ips": result.get("cluster_ips"),
            "external_ips": result.get("external_ips"),
            "external_name": result.get("external_name"),
            "selector": result.get("selector"),
            "ports": result.get("ports", []),
            "load_balancer": result.get("load_balancer"),
            "session_affinity": result.get("session_affinity"),
            "external_traffic_policy": result.get("external_traffic_policy"),
            "internal_traffic_policy": result.get("internal_traffic_policy"),
            "ip_families": result.get("ip_families"),
            "ip_family_policy": result.get("ip_family_policy"),
            "diagnostic_hint": result.get("diagnostic_hint"),
        }
        text = json.dumps(focused, default=str)
    else:
        text = json.dumps(result, default=str)

    max_chars = 8000 if tool in ("get_nodes", "investigate_node", "get_pods", "get_deployment", "list_namespace_resources", "get_endpoints", "get_service") else MAX_OBSERVATION_CHARS
    if len(text) > max_chars:
        text = text[:max_chars] + "...(truncated)"

    return sanitize_observation(text, max_chars)


TRIMMED_OBS_CHARS = 600
_TRIM_MARKER = "\n[... result truncated to fit context budget ...]"


def _trim_observations(observations: list[str]) -> None:
    """Condense observation bodies, oldest first, when over the context budget.

    Early findings usually anchor causal chains (pod → service → PVC), so
    steps are never dropped entirely — older observations are shortened
    to their head while the most recent observation stays intact whenever
    possible. The head keeps ``Step N — Tool: ...`` plus the start of the
    result, which is where statuses and error reasons usually appear.
    """
    total = sum(len(o) for o in observations)
    if total <= MAX_CONTEXT_CHARS:
        return

    # Condense from oldest forward, leaving the most recent observation alone.
    # Skip observations already condensed (idempotent) and any that are
    # already shorter than the condensed target.
    for i in range(len(observations) - 1):
        if total <= MAX_CONTEXT_CHARS:
            return
        obs = observations[i]
        if len(obs) <= TRIMMED_OBS_CHARS + len(_TRIM_MARKER):
            continue
        if obs.endswith(_TRIM_MARKER):
            continue
        condensed = obs[:TRIMMED_OBS_CHARS] + _TRIM_MARKER
        total -= len(obs) - len(condensed)
        observations[i] = condensed

    # Last resort: a single huge recent observation can still blow the budget.
    # Trim it too, but keep at least TRIMMED_OBS_CHARS so the LLM sees the
    # top of the latest tool result.
    if total > MAX_CONTEXT_CHARS and observations:
        last = observations[-1]
        allowed = max(TRIMMED_OBS_CHARS, MAX_CONTEXT_CHARS - (total - len(last)))
        if len(last) > allowed + len(_TRIM_MARKER):
            observations[-1] = last[:allowed] + _TRIM_MARKER


# ── Emergency fallback ───────────────────────────────────────────────────────

def _emergency_answer(steps: list[ReActStep], question: str) -> str:
    """Final-resort fallback when both the envelope-aware finalize *and* the
    critic streaming path have failed (LLM error, unparseable response, max
    iterations + finalize exception). Reads raw ``step.observation`` text
    because at this point the only goal is to return *something* to the user.
    Not part of the normal synthesis path — see ``stream_finalize_with_critic``."""
    if not steps:
        return (
            "I wasn't able to complete the investigation. "
            "Please try asking a more specific question, like "
            "\"investigate pod my-app in namespace staging\"."
        )

    # Collect all observations
    findings = []
    for step in steps:
        if step.observation and step.action != "answer":
            findings.append(f"**{step.action}**: {step.observation[:500]}")

    if findings:
        return (
            "I gathered some information but couldn't complete the full investigation. "
            "Here's what I found:\n\n" + "\n\n".join(findings[:5])
        )

    return (
        "The investigation didn't complete successfully. "
        "Try asking about a specific pod or namespace."
    )


def _should_replace_primary_result(current_tool: Optional[str], new_tool: str) -> bool:
    """Keep the user-requested root-cause target as the displayed result."""
    # Escape hatches/discovery tools should never be the primary tool
    if new_tool in {"get_namespaces", "find_workload", "kb_search"}:
        return False

    investigation_tools = {"investigate_pod", "investigate_workload", "analyze_namespace", "investigate_node"}
    if current_tool is None:
        # The first non-discovery tool becomes the primary tool
        return True

    if new_tool not in investigation_tools:
        return False
    # A pod investigation is the most specific root-cause result. Dependency
    # follow-up investigations should inform the answer, not replace the card.
    if current_tool == "investigate_pod" and new_tool != "investigate_pod":
        return False
    if new_tool == "investigate_pod":
        return True
    return current_tool not in investigation_tools


def _compact_log_excerpt(text: str, limit: int = 220) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _with_root_cause_summary(result: dict) -> dict:
    """Attach the Phase 6 deterministic root-cause card contract when possible."""
    if not isinstance(result, dict):
        return result
    if isinstance(result.get("root_cause_summary"), dict):
        return result
    summary = _build_root_cause_summary(result)
    if not summary:
        return result
    enriched = dict(result)
    enriched["root_cause_summary"] = summary
    return enriched


def _build_root_cause_summary(result: dict) -> Optional[dict]:
    evidence_summary = result.get("evidence_summary")
    evidence_summary = evidence_summary if isinstance(evidence_summary, dict) else {}
    evidence = result.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    classification = result.get("classification")
    classification = classification if isinstance(classification, dict) else {}

    target = (
        evidence.get("primary_target")
        or evidence.get("target")
        or {
            "pod_name": result.get("pod_name") or result.get("pod"),
            "namespace": result.get("namespace"),
            "mode": classification.get("mode"),
            "container": classification.get("container"),
        }
    )
    target = target if isinstance(target, dict) else {}

    source_tool = (
        _nested_get(result, ["_meta", "tool"])
        or _nested_get(result, ["meta", "tool"])
        or "investigate_pod"
    )
    resource_name = (
        target.get("pod_name")
        or target.get("name")
        or result.get("pod_name")
        or result.get("pod")
    )
    namespace = target.get("namespace") or result.get("namespace")
    mode = target.get("mode") or classification.get("mode")

    root_cause = str(evidence_summary.get("suspected_root_cause") or "").strip()
    suggested_fix = str(evidence_summary.get("suggested_fix") or "").strip()
    severity = ""
    root_candidate: Optional[dict] = None
    for item in evidence.get("failure_modes") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "verified_root_cause" or item.get("root_cause"):
            root_candidate = item
            root_cause = root_cause or str(item.get("root_cause") or "").strip()
            suggested_fix = suggested_fix or str(item.get("suggested_fix") or "").strip()
            severity = str(item.get("severity") or "").strip()
            break
    if not root_cause:
        return None
    if not resource_name or not namespace:
        return None

    confidence_signals = result.get("confidence_signals")
    confidence_signals = confidence_signals if isinstance(confidence_signals, dict) else {}
    data_completeness = str(confidence_signals.get("data_completeness") or "").strip() or "unknown"
    if not severity:
        severity = "critical" if str(mode) in {"CrashLoopBackOff", "ImagePullBackOff"} else "warning"

    evidence_items = _root_summary_evidence_items(evidence_summary, evidence, result)
    secondary_findings = _root_summary_secondary_findings(evidence_summary, evidence)
    related_resources = _root_summary_related_resources(evidence_summary, evidence)
    has_corroborating_evidence = bool(
        evidence_summary.get("dependency_checks")
        or result.get("container_log_findings")
        or related_resources
        or any(
            isinstance(item, dict) and item.get("evidence_priority") in {"dependency_check", "container_log_finding"}
            for item in evidence.get("contributing_factors") or []
        )
    )
    verified_deterministic = bool(root_candidate or (evidence_summary.get("suspected_root_cause") and has_corroborating_evidence))
    confidence = 0.95 if verified_deterministic else 0.75
    confidence_reason = (
        "Verified deterministic root cause from pod investigation with corroborating evidence."
        if verified_deterministic else
        "Deterministic pod investigation identified a suspected root cause, but corroborating evidence is limited."
    )

    return {
        "schema_version": "root_cause_summary.v1",
        "target": target,
        "resource_kind": "pod",
        "resource_name": resource_name,
        "namespace": namespace,
        "root_cause": root_cause,
        "severity": severity,
        "confidence": confidence,
        "confidence_reason": confidence_reason,
        "evidence": evidence_items,
        "secondary_findings": secondary_findings,
        "related_resources": related_resources,
        "suggested_fix": suggested_fix,
        "manual_actions": [suggested_fix] if suggested_fix else [],
        # Executable actions are intentionally populated by Phase 5 suggested_actions
        # only after deterministic validation, LLM review, and user approval gating.
        "executable_actions": [],
        "data_completeness": data_completeness,
        "source_tool": source_tool,
        "source_evidence": "verified_deterministic_investigation" if verified_deterministic else "deterministic_investigation",
    }


def _root_summary_evidence_items(evidence_summary: dict, evidence: dict, result: dict) -> list:
    items: list = []
    raw_items = evidence_summary.get("evidence")
    if isinstance(raw_items, list):
        items.extend(raw_items)
    for check in evidence_summary.get("dependency_checks") or []:
        if isinstance(check, dict):
            items.append({
                "type": "dependency_check",
                "target": check.get("target") or check.get("service"),
                "service": check.get("service"),
                "namespace": check.get("namespace"),
                "service_exists": check.get("service_exists"),
                "endpoints_exist": check.get("endpoints_exist"),
                "ready_addresses": check.get("ready_addresses"),
            })
    for finding in result.get("container_log_findings") or []:
        if not isinstance(finding, dict):
            continue
        previous = finding.get("logs_previous") if isinstance(finding.get("logs_previous"), dict) else {}
        current = finding.get("logs_current") if isinstance(finding.get("logs_current"), dict) else {}
        excerpt = _compact_log_excerpt(str(previous.get("excerpt") or current.get("excerpt") or ""))
        items.append({
            "type": "container_log_finding",
            "container": finding.get("container"),
            "reason": finding.get("reason") or finding.get("last_reason"),
            "restart_count": finding.get("restart_count"),
            "excerpt": excerpt,
        })
    for item in evidence.get("failure_modes") or []:
        if isinstance(item, dict) and item.get("root_cause"):
            items.append({
                "type": "verified_root_cause",
                "summary": item.get("root_cause"),
                "source": item.get("source"),
                "priority": item.get("evidence_priority"),
            })
    for item in evidence.get("contributing_factors") or []:
        if isinstance(item, dict):
            priority = item.get("evidence_priority")
            if priority in {"dependency_check", "container_log_finding"}:
                items.append(item)
    return items[:12]


def _root_summary_secondary_findings(evidence_summary: dict, evidence: dict) -> list:
    findings: list = []
    for item in evidence_summary.get("secondary_issues") or []:
        if isinstance(item, dict):
            findings.append(item)
    for item in evidence.get("contributing_factors") or []:
        if isinstance(item, dict) and item.get("evidence_priority") == "secondary_issue":
            findings.append(item)
    return findings[:8]


def _root_summary_related_resources(evidence_summary: dict, evidence: dict) -> list:
    resources: list = []
    for source in (evidence_summary.get("dependency_checks") or [], evidence.get("contributing_factors") or []):
        for item in source:
            if not isinstance(item, dict):
                continue
            service = item.get("service") or item.get("target")
            if service:
                resources.append({
                    "kind": "service",
                    "name": service,
                    "namespace": item.get("namespace"),
                    "relationship": "dependency",
                    "status": {
                        "service_exists": item.get("service_exists"),
                        "endpoints_exist": item.get("endpoints_exist"),
                        "ready_addresses": item.get("ready_addresses"),
                    },
                })
    deduped: list = []
    seen = set()
    for item in resources:
        key = (item.get("kind"), item.get("name"), item.get("namespace"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped[:8]


def _deterministic_investigate_pod_answer(result: Optional[dict]) -> str:
    if not isinstance(result, dict):
        return ""

    evidence_summary = result.get("evidence_summary")
    evidence_summary = evidence_summary if isinstance(evidence_summary, dict) else {}
    verified_root = str(evidence_summary.get("suspected_root_cause") or "").strip()
    container_findings = result.get("container_log_findings")
    container_findings = container_findings if isinstance(container_findings, list) else []

    if not verified_root and not container_findings:
        return ""

    pod = result.get("pod_name") or result.get("pod") or "the pod"
    namespace = result.get("namespace") or "the namespace"
    lines: list[str] = []

    if verified_root:
        lines.append(f"`{pod}` in namespace `{namespace}` is failing because **{verified_root}**")

    secondary = evidence_summary.get("secondary_issues")
    if isinstance(secondary, list):
        for issue in secondary[:3]:
            if not isinstance(issue, dict):
                continue
            container = issue.get("container")
            evidence = _compact_log_excerpt(str(issue.get("evidence") or ""))
            if container and evidence:
                lines.append(f"Secondary container issue: `{container}` shows `{evidence}`")

    finding_lines: list[str] = []
    for finding in container_findings[:4]:
        if not isinstance(finding, dict):
            continue
        container = finding.get("container")
        reason = finding.get("reason") or finding.get("last_reason") or ""
        previous = finding.get("logs_previous") if isinstance(finding.get("logs_previous"), dict) else {}
        current = finding.get("logs_current") if isinstance(finding.get("logs_current"), dict) else {}
        excerpt = _compact_log_excerpt(str(previous.get("excerpt") or current.get("excerpt") or ""))
        if not container or not (reason or excerpt):
            continue
        detail = f"`{container}`"
        if reason:
            detail += f" ({reason})"
        if excerpt:
            detail += f": {excerpt}"
        finding_lines.append(detail)

    if finding_lines:
        lines.append("Container findings: " + "; ".join(finding_lines))

    fix = str(evidence_summary.get("suggested_fix") or "").strip()
    if fix:
        lines.append(f"Recommended fix: {fix}")

    return "\n\n".join(lines)


# ── Action extraction (for frontend suggested actions) ───────────────────────

_EXECUTABLE_KUBECTL_PREFIXES = (
    "kubectl patch ", "kubectl apply ", "kubectl scale ",
    "kubectl rollout restart ", "kubectl rollout undo ",
    "kubectl delete pod ", "kubectl delete pods ",
    "kubectl set image ", "kubectl set resources ",
    "kubectl label ", "kubectl annotate ",
    "kubectl cordon ", "kubectl uncordon ", "kubectl drain ",
)

_HIGH_RISK_PREFIXES = (
    "kubectl delete ", "kubectl drain ", "kubectl cordon ",
    "kubectl patch ", "kubectl apply ",
)

_CLUSTER_SCOPED_KINDS = {
    "apiservice",
    "certificatesigningrequest",
    "clusterrole",
    "clusterrolebinding",
    "csidriver",
    "csinode",
    "customresourcedefinition",
    "gatewayclass",
    "ingressclass",
    "mutatingwebhookconfiguration",
    "namespace",
    "node",
    "persistentvolume",
    "podsecuritypolicy",
    "priorityclass",
    "runtimeclass",
    "storageclass",
    "validatingwebhookconfiguration",
    "volumeattachment",
}


def _extract_actions_from_steps(
    steps: list[ReActStep],
    last_result: Optional[dict],
    *,
    answer_text: str = "",
    reviewer_provider: Optional[Any] = None,
    question: str = "",
    usage_tracker: Optional[UsageTracker] = None,
) -> list:
    """Extract suggested kubectl *fix* commands from tool results.

    Only includes commands that the execute endpoint will actually accept
    (write operations like delete pod, rollout restart, scale, patch, etc.).
    Diagnostic / read-only commands (get, describe, logs) are filtered out —
    the investigation already ran those.
    """
    actions = []
    seen = set()
    envelopes = _dedupe_envelopes([(s.envelope, s.iteration) for s in steps if s.envelope is not None])
    evidence_priority = build_evidence_priority_summary(envelopes)

    def _add_cmd(c: str, desc: str, stdin: Optional[str] = None) -> None:
        candidate = _build_recovery_action_candidate(c, desc, stdin=stdin)
        reviewed = _review_recovery_action(
            candidate,
            evidence_priority=evidence_priority,
            reviewer_provider=reviewer_provider,
            question=question,
            answer_text=answer_text,
            usage_tracker=usage_tracker,
        )
        if not reviewed:
            return
        key = (reviewed["command"], reviewed.get("stdin") or "")
        if key in seen:
            return
        seen.add(key)
        actions.append(reviewed)

    # Extract from the last tool result
    if isinstance(last_result, dict):
        # From AI analysis
        ai = last_result.get("ai", {})
        if isinstance(ai, dict):
            ai_analysis = ai.get("ai_analysis", {})
            if isinstance(ai_analysis, dict):
                for cmd in ai_analysis.get("commands", []):
                    c = cmd if isinstance(cmd, str) else (cmd.get("command") or cmd.get("cmd") or "")
                    desc = "" if isinstance(cmd, str) else cmd.get("description", "")
                    _add_cmd(c, desc)

        # From fix commands
        for cmd in last_result.get("commands", []):
            c = cmd if isinstance(cmd, str) else (cmd.get("command") or cmd.get("cmd") or "")
            desc = "" if isinstance(cmd, str) else cmd.get("description", "")
            _add_cmd(c, desc)

    if answer_text:
        for yaml_text in _extract_apply_yaml_blocks(answer_text):
            _add_cmd("kubectl apply -f -", "Review and apply proposed YAML fix", stdin=yaml_text)

        for command in _extract_kubectl_commands(answer_text):
            _add_cmd(command, "Review and execute suggested fix")

    # Read-only "Trace source/config" follow-up. Emitted next to executable
    # action assembly but intentionally bypasses _add_cmd / _review_recovery_action:
    # it carries a follow_up_prompt the UI resubmits as chat, never a command.
    # Reserve a slot so the total stays capped at 5 (4 executable + 1 manual).
    manual_trace = _build_manual_trace_action(last_result, answer_text)
    if manual_trace:
        return actions[:4] + [manual_trace]
    return actions[:5]


def run_verification_sub_run(
    parent_run_id: str,
    action: str,
    params: dict,
    dispatch_fn: Callable[[str, dict], dict],
    provider: Any,
    parent_recorder: Optional[Any],
    context_mgr: Optional[Any],
) -> str:
    """Executes a verification sub-run after a successful mutating operation.

    Creates a child run in agent_runs table with route="verification" and
    parent_run_id set to parent_run_id. Executes read-only verification tools
    based on the action type, and uses the LLM to verify the state before/after
    the mutation.
    """
    import uuid
    import db
    from agent_run_recorder import AgentRunRecorder

    # 1. Determine the verification tools based on action type
    v_tools = []
    ns = params.get("namespace", "default")
    if action == "delete_pod":
        v_tools = [
            ("get_pods", {"namespace": ns}),
            ("get_events", {"namespace": ns}),
        ]
    elif action == "rollout_restart":
        v_tools = [
            ("get_rollout_status", {"namespace": ns, "deployment_name": params.get("deployment_name")}),
            ("get_pods", {"namespace": ns}),
            ("get_events", {"namespace": ns}),
        ]
    elif action == "scale_deployment":
        v_tools = [
            ("get_deployment", {"namespace": ns, "deployment_name": params.get("deployment_name")}),
            ("get_pods", {"namespace": ns}),
        ]
    elif action == "apply_patch":
        resource_type = params.get("resource_type", "")
        if resource_type and resource_type.lower() == "deployment":
            v_tools.append(("get_deployment", {"namespace": ns, "deployment_name": params.get("resource_name")}))
        v_tools.extend([
            ("get_pods", {"namespace": ns}),
            ("get_events", {"namespace": ns}),
        ])
    else:
        # Fallback to get_pods and get_events
        v_tools = [
            ("get_pods", {"namespace": ns}),
            ("get_events", {"namespace": ns}),
        ]

    # 2. Retrieve parent run info to inherit fields
    session_id = None
    user_id = None
    model = None
    model_params = None
    user_message_id = None
    if parent_run_id:
        try:
            parent_run = db.get_agent_run(parent_run_id)
            if parent_run:
                session_id = parent_run.get("session_id")
                user_id = parent_run.get("user_id")
                model = parent_run.get("model")
                model_params = parent_run.get("model_params_json")
                user_message_id = parent_run.get("user_message_id")
        except Exception as exc:
            logger.warning("Failed to get parent run info: %s", exc)

    # 3. Create the child run
    sub_run_id = str(uuid.uuid4())
    try:
        db.create_agent_run(
            run_id=sub_run_id,
            session_id=session_id,
            user_id=user_id,
            parent_run_id=parent_run_id,
            user_message_id=user_message_id,
            route="verification",
            model=model,
            model_params=model_params,
        )
    except Exception as exc:
        logger.warning("Failed to create child agent run: %s", exc)

    sub_recorder = AgentRunRecorder(
        run_id=sub_run_id,
        user_id=user_id,
        session_id=session_id,
    )

    # 4. Find the dry-run preview (before-state)
    dry_run_preview = ""
    if parent_run_id:
        try:
            steps_db = db.get_agent_steps(parent_run_id)
            for s in reversed(steps_db):
                if s["action"] == action and s["status"] in ("pending_approval", "ok"):
                    dry_run_preview = s["observation_preview"] or ""
                    break
        except Exception as exc:
            logger.warning("Failed to retrieve parent steps for dry-run preview: %s", exc)

    if not dry_run_preview:
        dry_run_preview = f"Executed {action} with parameters {json.dumps(params)}"

    # 5. Execute read-only verification tools and collect evidence
    gathered_evidence = []
    for idx, (v_tool, v_param) in enumerate(v_tools, start=1):
        step_start_time = time.perf_counter()
        try:
            dispatch_start = time.perf_counter()
            v_result = dispatch_fn(v_tool, v_param)
            dispatch_duration = time.perf_counter() - dispatch_start
            _record_tool_dispatch(v_tool, dispatch_duration, v_result)
            # Check for ToolEnvelope object
            try:
                from services.tool_envelope import ToolEnvelope
                if isinstance(v_result, ToolEnvelope):
                    v_result = v_result.model_dump(by_alias=True)
            except ImportError:
                pass
            error_code = v_result.get("error") if isinstance(v_result, dict) else None
        except Exception as exc:
            v_result = {"error": str(exc)}
            error_code = str(exc)

        duration = round((time.perf_counter() - step_start_time) * 1000)
        obs_text = _truncate_observation(v_result, v_tool)
        obs_id = str(uuid.uuid4())

        v_err_type = None
        v_err_msg = None
        if error_code:
            from agent_errors import classify_error
            v_err_type = classify_error(str(error_code), str(v_result.get("message") or "")).value
            v_err_msg = str(v_result.get("message") or error_code)

        step_id = sub_recorder.record_step(
            iteration=idx,
            action=v_tool,
            status="error" if error_code else "ok",
            step_kind="tool",
            thought=f"Verifying mutating action {action} by running {v_tool}",
            params=v_param,
            observation_preview=obs_text,
            observation_ref=obs_id,
            error_type=v_err_type,
            error_message=v_err_msg,
            duration_ms=duration,
        )

        # Save the full observation to db
        raw_result_str = ""
        if isinstance(v_result, dict):
            try:
                raw_result_str = json.dumps(v_result, default=str)
            except Exception:
                raw_result_str = str(v_result)
        else:
            raw_result_str = str(v_result)

        try:
            if context_mgr:
                redacted_content = context_mgr.redact_observation(raw_result_str)
                source, trust_level = context_mgr.get_tool_metadata(v_tool)
            else:
                from services.rag.redaction import redact
                redacted_content = redact(raw_result_str)
                source, trust_level = "system_discovery", "system"

            db.save_agent_observation(
                id=obs_id,
                run_id=sub_run_id,
                step_id=step_id,
                tool=v_tool,
                source=source,
                trust_level=trust_level,
                content_type="application/json",
                content=redacted_content,
                summary=None,
                redaction_status="redacted",
                bytes_in=len(raw_result_str),
                bytes_out=len(redacted_content),
            )
        except Exception as exc:
            logger.warning("Verification save_agent_observation failed: %s", exc)

        gathered_evidence.append((v_tool, v_param, obs_text))

    # 6. Generate the LLM verification report
    prompt = f"""You are a Kubernetes verification assistant. Your task is to verify whether a mutating command executed successfully by comparing the intended changes with the gathered cluster evidence.

Intended Action: {action}
Parameters: {json.dumps(params)}

Before-State Preview (Dry-Run / Proposed Change):
{dry_run_preview}

Gathered Cluster Evidence:
"""
    for v_tool, v_param, obs_text in gathered_evidence:
        prompt += f"\n--- Tool: {v_tool} with params {json.dumps(v_param)} ---\n"
        prompt += f"{obs_text}\n"

    prompt += """
Please analyze the evidence and generate a verification report.
Your report must include:
1. A summary of the before-state and after-state.
2. A clear verdict (SUCCESS, FAILED, or UNDETERMINED).
3. Any remaining risks, errors, or warnings observed in the logs/events.

Format the output as clean markdown.
"""

    system_prompt = "You are an expert Kubernetes operator that verifies if actions succeeded by inspecting cluster state."
    from services.llm.pricing import TokenUsage
    usage = TokenUsage.empty(model=getattr(provider, "model", ""))
    try:
        if hasattr(provider, "generate_with_usage"):
            report, usage = provider.generate_with_usage(
                prompt,
                system=system_prompt,
                temperature=0.1,
            )
        elif hasattr(provider, "generate"):
            report = provider.generate(
                prompt,
                system=system_prompt,
                temperature=0.1,
            )
        else:
            report, usage = _stream_with_usage(
                provider,
                prompt,
                system=system_prompt,
                temperature=0.1,
                max_tokens=2000,
            )
    except Exception as exc:
        logger.warning("Verification LLM call failed: %s", exc)
        report = f"Verification report generation failed: {exc}. Evidence gathered:\n" + "\n".join(
            f"- {v_tool}: {obs_text[:200]}" for v_tool, _, obs_text in gathered_evidence
        )

    # 7. Record the final answer and close the sub-run
    try:
        sub_recorder.finish(
            final_answer=report,
            final_tool=action,
            total_tokens_in=usage.tokens_in,
            total_tokens_out=usage.tokens_out,
            total_cached_tokens_in=usage.cached_tokens_in,
            total_cost_usd=usage.cost_usd,
        )
    except Exception as exc:
        logger.warning("Failed to finish verification run: %s", exc)

    return report


# ── "Trace source/config" manual follow-up action ────────────────────────────
#
# Strong indicators are inherently source/config-managed. Weak indicators
# (ConfigMap/Secret/env) are generic and only count when tied to a
# mount/reference/pin/version context, to avoid firing the button on every
# answer that merely mentions a ConfigMap.
_TRACE_STRONG_INDICATORS = (
    "helm", "chart", "values.yaml", "values file", "helm values",
    "ansible", "playbook", "manifest", "deployment repo", "deployment_repo",
    "plugin", "pinned", "version pin", "dependency version", "image tag",
)
_TRACE_WEAK_INDICATORS = (
    "configmap", "config map", "secret", "env var", "environment variable",
)
_TRACE_WEAK_CONTEXT = (
    "mount", "volume", "reference", "referenced", "pin", "pinned",
    "version", "defined in", "set in", "configured in", "declared in",
)
_TRACE_EXCERPT_CAP = 240
_TRACE_VERSION_RE = re.compile(r"\b\d+\.\d+(?:\.\d+)?(?:[-.][A-Za-z0-9]+)*\b")


def _detect_source_indicators(text: str) -> list[str]:
    """Narrowly detect source-managed indicator terms in text."""
    low = (text or "").lower()
    found: list[str] = []
    for term in _TRACE_STRONG_INDICATORS:
        if term in low and term not in found:
            found.append(term)
    if any(ctx in low for ctx in _TRACE_WEAK_CONTEXT):
        for term in _TRACE_WEAK_INDICATORS:
            if term in low and term not in found:
                found.append(term)
    return found


def _extract_version_names(text: str, limit: int = 5) -> list[str]:
    seen: list[str] = []
    for match in _TRACE_VERSION_RE.findall(text or ""):
        if match not in seen:
            seen.append(match)
        if len(seen) >= limit:
            break
    return seen


def _build_manual_trace_action(last_result: Optional[dict], answer_text: str) -> Optional[dict]:
    """Build a read-only "Trace source/config" follow-up action, or None.

    Emitted only when (a) the result carries a verified deterministic
    root-cause summary, and (b) the root cause / suggested fix points at
    source-managed configuration. This is NOT an executable action: it has a
    ``follow_up_prompt`` the UI submits as a new chat message, no ``command``
    and no ``confirm``.
    """
    if not isinstance(last_result, dict):
        return None
    summary = last_result.get("root_cause_summary")
    if not isinstance(summary, dict):
        return None

    confidence = summary.get("confidence")
    verified = summary.get("source_evidence") == "verified_deterministic_investigation" or (
        isinstance(confidence, (int, float)) and confidence >= 0.95
    )
    if not verified:
        return None

    root_cause = str(summary.get("root_cause") or "").strip()
    if not root_cause:
        return None
    suggested_fix = str(summary.get("suggested_fix") or "").strip()

    indicator_text = " ".join(filter(None, [root_cause, suggested_fix, answer_text or ""]))
    indicators = _detect_source_indicators(indicator_text)
    if not indicators:
        return None

    resource_name = str(summary.get("resource_name") or "").strip()
    namespace = str(summary.get("namespace") or "").strip()
    target = summary.get("target") if isinstance(summary.get("target"), dict) else {}
    container = str((target or {}).get("container") or "").strip()
    versions = _extract_version_names(indicator_text)
    error_excerpt = (root_cause or suggested_fix)[:_TRACE_EXCERPT_CAP]

    locator = resource_name or "the affected workload"
    qualifiers = []
    if namespace:
        qualifiers.append(f"namespace {namespace}")
    if container:
        qualifiers.append(f"container {container}")
    resource_line = f"Resource: {locator}" + (f" ({', '.join(qualifiers)})" if qualifiers else "")

    context_lines = [resource_line, f"Root cause: {error_excerpt}"]
    if indicators:
        context_lines.append("Suspected source/config: " + ", ".join(indicators))
    if versions:
        context_lines.append("Relevant names/versions: " + ", ".join(versions))

    search_hint = f" (search for {', '.join(versions)})" if versions else ""
    follow_up_prompt = (
        "Trace where the durable fix for this verified root cause lives in "
        "source/config. This is a read-only investigation only.\n\n"
        + "\n".join(context_lines)
        + "\n\nInvestigate in this order, read-only:\n"
        "1. Inspect the workload's mounted ConfigMaps and env references "
        "(investigate_workload, list_namespace_resources).\n"
        "2. Use search_configmaps in this namespace to find which ConfigMap and "
        f"key defines the failing value{search_hint}, then get_configmap to read "
        "that key.\n"
        "3. For chart/values provenance, use get_helm_release (after "
        "list_helm_releases / helm_available) to read the release's Helm values "
        "and rendered manifest — Helm values are closer to source than the "
        "rendered ConfigMap.\n"
        "4. If the live config does not explain it, use kb_search for the owning "
        "source file or runbook.\n"
        "Report exact ConfigMap names, keys, Helm release/chart names, and "
        "resource names. Never read Secret values. If you cannot find a matching "
        "source, say the source location is not verified and do not invent a file path."
    )

    return {
        "type": "trace",
        "action_kind": "manual",
        "label": "Trace source/config",
        "follow_up_prompt": follow_up_prompt,
        "evidence_reference": {
            "schema": summary.get("schema_version"),
            "resource_name": resource_name or None,
            "namespace": namespace or None,
            "root_cause": root_cause[:_TRACE_EXCERPT_CAP],
            "source_evidence": summary.get("source_evidence"),
            "confidence": confidence,
            "indicators": indicators,
        },
    }


def _build_recovery_action_candidate(command: str, label: str, *, stdin: Optional[str] = None) -> dict:
    command = _normalize_executable_kubectl_command(command)
    action_kind = "apply_yaml" if command == "kubectl apply -f -" else "write_command"
    return {
        "type": "apply",
        "action_kind": action_kind,
        "label": label or command[:60],
        "command": command,
        "stdin": stdin,
    }


def _review_recovery_action(
    action: dict,
    *,
    evidence_priority: dict,
    reviewer_provider: Optional[Any],
    question: str,
    answer_text: str,
    usage_tracker: Optional[UsageTracker] = None,
) -> Optional[dict]:
    deterministic = _deterministic_review_recovery_action(action, evidence_priority)
    if not deterministic.get("approved"):
        return None
    if not _llm_review_recovery_action(
        action,
        deterministic,
        reviewer_provider,
        question,
        answer_text,
        usage_tracker=usage_tracker,
    ):
        return None

    reviewed = dict(action)
    reviewed.update({
        "type": "apply",
        "action_kind": deterministic["action_kind"],
        "risk": deterministic["risk"],
        "requires_approval": True,
        "confirm": True,
        "evidence_reference": deterministic["evidence_reference"],
        "review": {
            "deterministic": "passed",
            "llm": "approved",
        },
    })
    if not reviewed.get("stdin"):
        reviewed.pop("stdin", None)
    return reviewed


def _deterministic_review_recovery_action(
    action: dict,
    evidence_priority: dict,
    *,
    require_evidence: bool = True,
) -> dict:
    command = _normalize_executable_kubectl_command(str(action.get("command") or ""))
    stdin = action.get("stdin")
    if not command.startswith("kubectl"):
        return {"approved": False, "reason": "not_kubectl"}
    if not any(command.startswith(prefix) for prefix in _EXECUTABLE_KUBECTL_PREFIXES):
        return {"approved": False, "reason": "not_allowlisted"}
    if any(ch in command for ch in ";|&$`()"):
        return {"approved": False, "reason": "shell_metacharacter"}
    if command == "kubectl apply -f -" and not str(stdin or "").strip():
        return {"approved": False, "reason": "apply_stdin_missing"}

    action_kind = "apply_yaml" if command == "kubectl apply -f -" else "write_command"
    risk = _recovery_action_risk(command)
    if action_kind == "apply_yaml":
        target = _yaml_action_target(str(stdin or ""))
        if target.get("invalid_documents"):
            return {"approved": False, "reason": "yaml_invalid_document"}
        if not target.get("kind") or not target.get("name"):
            return {"approved": False, "reason": "yaml_target_missing"}
        missing_namespace = [
            doc for doc in target.get("documents") or [target]
            if _yaml_document_requires_namespace(doc) and not doc.get("namespace")
        ]
        if missing_namespace:
            return {"approved": False, "reason": "yaml_namespace_missing"}
    else:
        target = _kubectl_action_target(command)
        if not target.get("resource") or not target.get("name"):
            return {"approved": False, "reason": "command_target_missing"}
        if _kubectl_action_requires_namespace(command) and not target.get("namespace"):
            return {"approved": False, "reason": "namespace_missing"}

    evidence_ref = (evidence_priority or {}).get("primary_root_cause")
    if require_evidence and not evidence_ref:
        return {"approved": False, "reason": "missing_evidence_reference"}

    if require_evidence and risk == "high" and evidence_ref.get("priority") not in {"verified_root_cause", "primary_failure"}:
        return {"approved": False, "reason": "high_risk_not_supported"}

    return {
        "approved": True,
        "action_kind": action_kind,
        "risk": risk,
        "target": target,
        "evidence_reference": {
            "priority": evidence_ref.get("priority") if evidence_ref else None,
            "label": evidence_ref.get("label") if evidence_ref else None,
            "summary": evidence_ref.get("summary") if evidence_ref else None,
            "source_tool": evidence_ref.get("source_tool") if evidence_ref else None,
            "evidence_path": evidence_ref.get("evidence_path") if evidence_ref else None,
        },
    }


def _llm_review_recovery_action(
    action: dict,
    deterministic_review: dict,
    provider: Optional[Any],
    question: str,
    answer_text: str,
    usage_tracker: Optional[UsageTracker] = None,
) -> bool:
    """Separate reviewer for executable actions. Fail closed if unavailable."""
    if provider is None:
        return False
    prompt = (
        "Review this Kubernetes recovery action independently from the prose answer. "
        "Return ONLY JSON: {\"approved\": true|false, \"reason\": \"...\"}.\n\n"
        f"User question: {question}\n"
        f"Action: {json.dumps(action, default=str)}\n"
        f"Deterministic review: {json.dumps(deterministic_review, default=str)}\n"
        f"Answer excerpt: {(answer_text or '')[:2000]}\n\n"
        "Approve only if the action is evidence-supported, targets a concrete resource and namespace when applicable, "
        "is allowlist-compatible, proportional to the diagnosis, and requires approval."
    )
    from services.llm.pricing import TokenUsage
    usage = TokenUsage.empty(model=getattr(provider, "model", ""))
    try:
        if hasattr(provider, "generate_with_usage"):
            raw, usage = provider.generate_with_usage(prompt, system="You are a strict Kubernetes action safety reviewer.", temperature=0.0, max_tokens=600)
        elif hasattr(provider, "generate"):
            raw = provider.generate(prompt, system="You are a strict Kubernetes action safety reviewer.", temperature=0.0, max_tokens=600)
        else:
            raw, usage = _stream_with_usage(
                provider,
                prompt,
                system="You are a strict Kubernetes action safety reviewer.",
                temperature=0.0,
                max_tokens=600,
            )
        parsed = _json_object_from_text(raw)
    except Exception:
        if usage_tracker is not None:
            usage_tracker.add(usage)
        return False
    if usage_tracker is not None:
        usage_tracker.add(usage)
    return bool(isinstance(parsed, dict) and parsed.get("approved") is True)


def _recovery_action_risk(command: str) -> str:
    if command.startswith(_HIGH_RISK_PREFIXES):
        return "high"
    if command.startswith(("kubectl scale ", "kubectl rollout restart ", "kubectl rollout undo ", "kubectl set ")):
        return "medium"
    return "low"


def _kubectl_action_requires_namespace(command: str) -> bool:
    return not command.startswith(("kubectl cordon ", "kubectl uncordon ", "kubectl drain "))


def _kubectl_action_target(command: str) -> dict:
    try:
        tokens = shlex.split(command)
    except Exception:
        return {}
    target = {"resource": "", "name": "", "namespace": ""}
    for idx, token in enumerate(tokens):
        if token in ("-n", "--namespace") and idx + 1 < len(tokens):
            target["namespace"] = tokens[idx + 1]
        elif token.startswith("--namespace="):
            target["namespace"] = token.split("=", 1)[1]

    if len(tokens) >= 4 and tokens[:3] == ["kubectl", "rollout", "restart"]:
        _fill_resource_name(target, tokens[3:])
    elif len(tokens) >= 4 and tokens[:3] == ["kubectl", "rollout", "undo"]:
        _fill_resource_name(target, tokens[3:])
    elif len(tokens) >= 4 and tokens[:3] == ["kubectl", "delete", "pod"]:
        target["resource"] = "pod"
        if not tokens[3].startswith("-"):
            target["name"] = tokens[3]
    elif len(tokens) >= 4 and tokens[:3] == ["kubectl", "delete", "pods"]:
        target["resource"] = "pod"
        if not tokens[3].startswith("-"):
            target["name"] = tokens[3]
    elif len(tokens) >= 4 and tokens[0:2] in (["kubectl", "patch"], ["kubectl", "scale"], ["kubectl", "label"], ["kubectl", "annotate"]):
        _fill_resource_name(target, tokens[2:])
    elif len(tokens) >= 5 and tokens[:3] == ["kubectl", "set", "image"]:
        _fill_resource_name(target, tokens[3:])
    elif len(tokens) >= 5 and tokens[:3] == ["kubectl", "set", "resources"]:
        _fill_resource_name(target, tokens[3:])
    elif len(tokens) >= 3 and tokens[0] == "kubectl" and tokens[1] in {"cordon", "uncordon", "drain"}:
        target["resource"] = "node"
        if not tokens[2].startswith("-"):
            target["name"] = tokens[2]
    return target


def _fill_resource_name(target: dict, tokens: list[str]) -> None:
    meaningful = []
    for token in tokens:
        if not token:
            continue
        if token.startswith("-"):
            break
        meaningful.append(token)
    if not meaningful:
        return
    first = meaningful[0]
    if "/" in first:
        resource, name = first.split("/", 1)
    elif len(meaningful) >= 2:
        resource, name = meaningful[0], meaningful[1]
    else:
        return
    target["resource"] = resource
    target["name"] = name


def _yaml_action_target(yaml_text: str) -> dict:
    target = {"kind": "", "name": "", "namespace": "", "documents": []}
    if yaml is not None:
        try:
            for document in yaml.safe_load_all(yaml_text or ""):
                if document is None:
                    continue
                if not isinstance(document, dict):
                    target["invalid_documents"] = True
                    continue
                metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
                doc_target = {
                    "kind": str(document.get("kind") or "").strip(),
                    "name": str(metadata.get("name") or "").strip(),
                    "namespace": str(metadata.get("namespace") or "").strip(),
                }
                if not doc_target["kind"] or not doc_target["name"]:
                    target["invalid_documents"] = True
                target["documents"].append(doc_target)
            if target["documents"]:
                target.update(target["documents"][0])
                return target
        except Exception:
            target = {"kind": "", "name": "", "namespace": "", "documents": []}

    kind_match = re.search(r"(?m)^kind:\s*([A-Za-z0-9_.-]+)\s*$", yaml_text or "")
    if kind_match:
        target["kind"] = kind_match.group(1)
    name_match = re.search(r"(?m)^\s{2}name:\s*([A-Za-z0-9_.-]+)\s*$", yaml_text or "")
    if name_match:
        target["name"] = name_match.group(1)
    ns_match = re.search(r"(?m)^\s{2}namespace:\s*([A-Za-z0-9_.-]+)\s*$", yaml_text or "")
    if ns_match:
        target["namespace"] = ns_match.group(1)
    if target["kind"] or target["name"] or target["namespace"]:
        target["documents"] = [{
            "kind": target["kind"],
            "name": target["name"],
            "namespace": target["namespace"],
        }]
    return target


def _yaml_document_requires_namespace(target: dict) -> bool:
    kind = str((target or {}).get("kind") or "").strip().lower()
    if not kind:
        return True
    return kind not in _CLUSTER_SCOPED_KINDS


def _extract_apply_yaml_blocks(answer_text: str) -> list[str]:
    """Return YAML payloads from fenced blocks marked with '# patch:apply'."""
    blocks: list[str] = []
    pattern = re.compile(
        r"```(?:ya?ml)\s*\n#\s*patch:apply\s*\n(.*?)```",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(answer_text or ""):
        yaml_text = match.group(1).strip()
        if yaml_text:
            blocks.append(yaml_text)
    return blocks


def _extract_kubectl_commands(answer_text: str) -> list[str]:
    """Extract simple one-line kubectl commands from synthesized Markdown."""
    commands: list[str] = []
    text = answer_text or ""

    candidates = re.findall(r"`(kubectl\s+[^`\n]+)`", text)
    candidates.extend(re.findall(r"(?m)^\s*(kubectl\s+[^\n]+)$", text))

    for candidate in candidates:
        command = _normalize_executable_kubectl_command(candidate)
        if command:
            commands.append(command)
    return commands


def _normalize_executable_kubectl_command(candidate: str) -> str:
    """Trim Markdown/prose suffixes from a one-line kubectl command candidate."""
    command = (candidate or "").strip().rstrip(".")
    command = re.sub(
        r"\s+\((?:requires approval|approval required|requires review|review required|needs approval|confirm before running)\)\s*$",
        "",
        command,
        flags=re.IGNORECASE,
    )
    command = re.sub(
        r"\s+-\s*(?:requires approval|approval required|requires review|review required|needs approval|confirm before running)\s*$",
        "",
        command,
        flags=re.IGNORECASE,
    )
    return command.strip()
