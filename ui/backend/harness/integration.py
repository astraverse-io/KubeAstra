"""Bridge the native tool-calling loop (loop.py) into react_loop's contract.

``run_native_react`` adapts :func:`harness.loop.run_native_tool_loop` so that,
from the outside, a native run looks exactly like a text-ReAct run:

- it returns a ``ReActResult`` (answer + steps + totals),
- it emits the same ``on_event`` SSE shapes the text harness uses
  (``step_complete`` per tool call, ``answer_start`` / ``token`` / ``answer_end``),
- it records steps and the final rollup through ``agent_run_recorder``,
- tool observations go through ``react._truncate_observation`` → the same
  envelope + ``sanitize_observation`` secret-scrubbing as the text path.

Called from ``react_loop`` only when ``AGENT_HARNESS_V2`` is set and the provider
reports ``supports_native_tools()``; everything else stays on text-ReAct. Heavy
imports are deferred to call time because ``react`` imports this module lazily.

Known native-mode limitations (v1; off by default, to be closed as the harness
matures and measured by Phase-4 evals):

- **No approval-resume / write-approval UX.** ``react_loop`` falls back to the
  text harness whenever ``resume_run_id`` / ``approved_token`` is set, so the
  Phase-2 approve-then-resume flow always runs on text-ReAct. Native mode also
  does not emit ``write_proposed`` / ``approval_required`` events.
- **Answer is not streamed token-by-token** — it arrives as a single ``token``
  event followed by ``answer_end``.
- **Minimal system prompt** compared with the tuned text-ReAct prompt (tool
  selection heuristics, safety guidance); quality delta is a Phase-4 question.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from .loop import LoopResult, StepRecord, run_native_tool_loop

logger = logging.getLogger(__name__)

# Native tool-calling needs no "emit action JSON" instructions (the text-ReAct
# prompt's whole middle section) — tools are passed structurally — so the system
# prompt only sets role and investigative intent.
_NATIVE_SYSTEM = (
    "You are KubeAstra, an expert Kubernetes investigation assistant. Use the "
    "provided tools to gather evidence before you answer: call tools to inspect "
    "the cluster, then give a clear, specific answer grounded in what the tools "
    "returned. Do not guess when a tool can tell you."
)


def _recent_conversation(history: Optional[list]) -> str:
    """Flatten the last few history turns into a context string.

    Mirrors the text harness (``history[-4:]``, 200 chars/turn): native tool
    APIs want structured alternating turns and would choke on tool-result
    interleaving, so recent conversation rides in the system preamble instead —
    same fidelity as the text path, no alternation hazard.
    """
    if not history:
        return ""
    recent = history[-4:]
    lines = [
        f"{getattr(m, 'role', 'user')}: {str(getattr(m, 'content', m))[:200]}"
        for m in recent
    ]
    return "Recent conversation:\n" + "\n".join(lines)


def run_native_react(
    *,
    question: str,
    provider: Any,
    dispatch_fn: Callable[[str, dict], dict],
    on_event: Optional[Callable[[dict], None]] = None,
    run_recorder: Optional[Any] = None,
    tool_scope: Optional[set] = None,
    memory_preamble: str = "",
    grounded_preamble: str = "",
    history: Optional[list] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
    max_steps: int = 12,
) -> Any:
    """Run one native tool-calling investigation and return a ``ReActResult``."""
    # Deferred imports: react imports this module, so import react lazily here.
    from react import ReActResult, ReActStep, _truncate_observation
    from agent_run_recorder import finish as _rec_finish
    from agent_run_recorder import record as _rec_step
    from services.llm.pricing import TokenUsage
    from tool_registry import build_native_tool_specs

    tools = build_native_tool_specs(allowed_tools=tool_scope)

    # Fold RAG/memory/recent-conversation context into the system preamble (the
    # text harness does the same; keeping it out of the turn list avoids native
    # role-alternation hazards).
    preamble = "\n\n".join(
        p for p in (grounded_preamble, memory_preamble, _recent_conversation(history)) if p
    )
    system = f"{_NATIVE_SYSTEM}\n\n{preamble}" if preamble else _NATIVE_SYSTEM

    steps: list[Any] = []
    started = time.monotonic()

    def _emit(event: dict) -> None:
        if on_event:
            try:
                on_event(event)
            except Exception:  # progress reporting must never affect correctness
                pass

    def execute_tool(name: str, arguments: dict) -> str:
        result = dispatch_fn(name, arguments)
        try:
            return _truncate_observation(result, name)
        except Exception:  # defensive: never let formatting crash a run
            logger.warning("native harness: observation formatting failed for %s", name)
            return str(result)

    def on_step(rec: StepRecord) -> None:
        step = ReActStep(
            iteration=rec.index + 1,
            thought=rec.thought,
            action=rec.tool_name,
            action_params=rec.arguments,
            observation=rec.observation,
        )
        steps.append(step)
        _rec_step(
            run_recorder,
            iteration=step.iteration,
            action=step.action,
            status="error" if rec.error else "ok",
            step_kind="tool",
            thought=step.thought,
            observation_preview=rec.observation,
        )
        _emit(
            {
                "type": "step_complete",
                "iteration": step.iteration,
                "thought": step.thought,
                "action": step.action,
                "action_params": step.action_params,
                "observation": rec.observation,
                "error": rec.error,
            }
        )

    def on_answer(answer: str) -> None:
        _emit({"type": "answer_start"})
        if answer:
            _emit({"type": "token", "content": answer})
        _emit({"type": "answer_end", "answer": answer})

    result: LoopResult = run_native_tool_loop(
        provider=provider,
        question=question,
        tools=tools,
        execute_tool=execute_tool,
        system=system,
        max_steps=max_steps,
        on_step=on_step,
        on_answer=on_answer,
        is_cancelled=is_cancelled,
    )

    usage = result.usage or TokenUsage()
    last_tool = steps[-1].action if steps else ""
    _rec_finish(
        run_recorder,
        final_answer=result.answer,
        final_tool=last_tool,
        total_tokens_in=usage.tokens_in,
        total_tokens_out=usage.tokens_out,
        total_cached_tokens_in=usage.cached_tokens_in,
        total_cost_usd=usage.cost_usd,
    )

    return ReActResult(
        answer=result.answer,
        tool_used=last_tool,
        steps=steps,
        total_iterations=len(steps),
        total_duration_ms=(time.monotonic() - started) * 1000.0,
    )
