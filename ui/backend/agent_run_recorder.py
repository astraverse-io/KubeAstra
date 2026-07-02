"""AgentRunRecorder — persistence wrapper for ReAct loop traces (harness Phase 1).

The recorder owns one ``agent_runs`` row. Callers feed it per-step metadata via
``record_step`` and close it with ``finish`` or ``fail``. Every DB call is
wrapped: a persistence failure logs a warning but never propagates, so the chat
flow keeps working when the trace table is unhappy.

A ``None`` recorder is a valid sentinel for "tracing disabled" — callers can
treat ``recorder.record_step(...)`` and friends as safe no-ops by going through
``record(recorder, ...)`` module-level helpers below.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import db

logger = logging.getLogger(__name__)


_OBSERVATION_PREVIEW_LIMIT = 4096
_PARAMS_REDACT_KEYS = {
    "password", "token", "secret", "auth", "authorization",
    "api_key", "apikey", "credential", "private_key",
}


def _redact_text(value: Optional[str]) -> Optional[str]:
    """Best-effort redaction using the MCP regex set; passthrough on failure."""
    if not value:
        return value
    try:
        from services.rag.redaction import redact  # type: ignore
        return redact(value)
    except Exception:
        return value


def _redact_value(v: Any) -> Any:
    """Recursively redact strings/dicts/lists; pass other types through."""
    if isinstance(v, str):
        return _redact_text(v)
    if isinstance(v, dict):
        return _redact_params(v)
    if isinstance(v, list):
        return [_redact_value(x) for x in v]
    return v


def _redact_params(params: Optional[dict]) -> Optional[dict]:
    """Strip obvious secret-shaped keys and redact string/dict/list values."""
    if not isinstance(params, dict):
        return params
    out: dict = {}
    for k, v in params.items():
        if str(k).lower() in _PARAMS_REDACT_KEYS:
            out[k] = "<REDACTED>"
            continue
        out[k] = _redact_value(v)
    return out


def _redact_preview(value: Any, limit: int = _OBSERVATION_PREVIEW_LIMIT) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        try:
            import json
            value = json.dumps(value, default=str)
        except Exception:
            value = str(value)
    redacted = _redact_text(value) or value
    if len(redacted) > limit:
        return redacted[:limit] + "\n...[truncated]"
    return redacted


@dataclass
class AgentRunRecorder:
    """Thin facade over the agent_runs / agent_steps db helpers."""

    run_id: str
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    route: Optional[str] = None
    _started_at: float = field(default_factory=time.perf_counter)
    _enabled: bool = True
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_cached_tokens_in: int = 0
    total_cost_usd: float = 0.0

    @classmethod
    def start(
        cls,
        *,
        session_id: Optional[str],
        user_id: Optional[str],
        route: Optional[str] = None,
        model: Optional[str] = None,
        model_params: Optional[dict] = None,
        parent_run_id: Optional[str] = None,
        user_message_id: Optional[int] = None,
        rag_decision: Optional[dict] = None,
        rag_sources: Optional[list] = None,
        tool_scope: Optional[Any] = None,  # canonical Phase 7: ScopeDecision.to_dict()
        concurrency_key: Optional[str] = None,
        labels: Optional[dict] = None,
        retention_policy: str = "standard",
        run_id: Optional[str] = None,
        system_prompt_sha: Optional[str] = None,
        react_system_sha: Optional[str] = None,
        tool_registry_sha: Optional[str] = None,
    ) -> Optional["AgentRunRecorder"]:
        """Open a new run and return a recorder. Returns None if persistence fails."""
        try:
            rid = db.create_agent_run(
                run_id=run_id,
                session_id=session_id,
                user_id=user_id,
                parent_run_id=parent_run_id,
                user_message_id=user_message_id,
                route=route,
                model=model,
                model_params=model_params,
                rag_decision=rag_decision,
                rag_sources=rag_sources,
                tool_scope=tool_scope,
                concurrency_key=concurrency_key,
                labels=labels,
                retention_policy=retention_policy,
                system_prompt_sha=system_prompt_sha,
                react_system_sha=react_system_sha,
                tool_registry_sha=tool_registry_sha,
            )
            return cls(run_id=rid, user_id=user_id, session_id=session_id, route=route)
        except Exception as exc:
            logger.warning("agent_run_recorder: failed to open run: %s", exc)
            return None

    def record_step(
        self,
        *,
        iteration: int,
        action: str,
        status: str,
        step_kind: str = "tool",
        thought: Optional[str] = None,
        params: Optional[dict] = None,
        source: Optional[str] = None,
        observation_preview: Any = None,
        observation_ref: Optional[str] = None,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        duration_ms: Optional[int] = None,
        tokens_in: int = 0,
        cached_tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        step_model: Optional[str] = None,
    ) -> Optional[int]:
        """Append one step. Never raises — logs and swallows DB errors."""
        if not self._enabled:
            return None
        try:
            return db.record_agent_step(
                run_id=self.run_id,
                iteration=iteration,
                step_kind=step_kind,
                action=action,
                status=status,
                thought=_redact_text(thought),
                params=_redact_params(params),
                source=source,
                observation_ref=observation_ref,
                observation_preview=_redact_preview(observation_preview),
                error_type=error_type,
                error_message=_redact_text(error_message),
                duration_ms=duration_ms,
                tokens_in=tokens_in,
                cached_tokens_in=cached_tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                step_model=step_model,
            )
        except Exception as exc:
            logger.warning("agent_run_recorder: record_step failed run=%s iter=%d: %s",
                           self.run_id, iteration, exc)
            return None

    def finish(
        self,
        *,
        final_answer: Optional[str],
        final_tool: Optional[str] = None,
        total_tokens_in: int = 0,
        total_tokens_out: int = 0,
        total_cached_tokens_in: int = 0,
        total_cost_usd: float = 0.0,
    ) -> None:
        """Close the run as complete. Never raises."""
        self.total_tokens_in = total_tokens_in
        self.total_tokens_out = total_tokens_out
        self.total_cached_tokens_in = total_cached_tokens_in
        self.total_cost_usd = total_cost_usd
        if not self._enabled:
            return
        try:
            db.finish_agent_run(
                self.run_id,
                final_answer=final_answer,
                final_tool=final_tool,
                total_tokens_in=total_tokens_in,
                total_tokens_out=total_tokens_out,
                total_cached_tokens_in=total_cached_tokens_in,
                total_cost_usd=total_cost_usd,
            )
        except Exception as exc:
            logger.warning("agent_run_recorder: finish failed run=%s: %s",
                           self.run_id, exc)
        finally:
            self._enabled = False

    def fail(
        self,
        *,
        error: str,
        status: str = "failed",
        final_answer: Optional[str] = None,
        final_tool: Optional[str] = None,
        total_tokens_in: int = 0,
        total_tokens_out: int = 0,
        total_cached_tokens_in: int = 0,
        total_cost_usd: float = 0.0,
    ) -> None:
        """Mark the run failed or aborted with cost and token metrics. Never raises."""
        self.total_tokens_in = total_tokens_in
        self.total_tokens_out = total_tokens_out
        self.total_cached_tokens_in = total_cached_tokens_in
        self.total_cost_usd = total_cost_usd
        if not self._enabled:
            return
        try:
            db.fail_agent_run(
                self.run_id,
                error=error,
                status=status,
                final_answer=final_answer,
                final_tool=final_tool,
                total_tokens_in=total_tokens_in,
                total_tokens_out=total_tokens_out,
                total_cached_tokens_in=total_cached_tokens_in,
                total_cost_usd=total_cost_usd,
            )
        except Exception as exc:
            logger.warning("agent_run_recorder: fail failed run=%s: %s",
                           self.run_id, exc)
        finally:
            self._enabled = False



# ── Module-level safe wrappers ────────────────────────────────────────────────
#
# Pattern: callers that may or may not have a recorder can write
#   record(recorder, iteration=1, action="get_pods", status="ok", ...)
# and not worry about null checks. Keeps ReAct call sites tidy.

def record(recorder: Optional[AgentRunRecorder], **kwargs) -> Optional[int]:
    if recorder is not None:
        return recorder.record_step(**kwargs)
    return None


def finish(recorder: Optional[AgentRunRecorder], **kwargs) -> None:
    if recorder is not None:
        recorder.finish(**kwargs)


def fail(recorder: Optional[AgentRunRecorder], **kwargs) -> None:
    if recorder is not None:
        recorder.fail(**kwargs)
