"""Agent Harness v2 — the native tool-calling loop (design §6).

Replaces the text-ReAct parse → regex-salvage → recovery-retry stack with a
structured loop: the provider returns first-class ``{name, arguments}`` tool
calls (:class:`services.llm.base.ToolTurn`), we execute each through an injected
executor, feed the observations back, and repeat until the model answers or a
step cap is hit. Because the arguments arrive already decoded, the "unparseable
action" failure class — and the salvage layers that exist to paper over it —
simply does not apply here.

Everything the loop depends on is injected:

- ``provider``   — anything implementing ``supports_native_tools`` /
  ``generate_with_tools`` (in production: the ``TracedProviderProxy`` wrapping a
  real provider, so tracing/usage still flow through).
- ``execute_tool`` — ``(name, arguments) -> observation_text``. Production wraps
  ``tool_registry.dispatch`` with a ``DispatchContext``; tests inject a fake.
- ``tools``      — provider-neutral specs from
  ``tool_registry.build_native_tool_specs``. Passing a *scoped* subset
  (``build_native_tool_specs(allowed_tools=...)``) is how a sub-agent / skill
  runs with its own tool list.
- optional ``on_step`` / ``on_answer`` hooks — production bridges these to
  ``agent_run_recorder`` + ``audit`` + the SSE stream; they default to no-ops.

This keeps the module unit-testable with no network and no FastAPI app, and
leaves the heavy collaborators (recorder, usage metrics, audit) to the wiring
layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from services.llm.pricing import TokenUsage

logger = logging.getLogger(__name__)

ExecuteTool = Callable[[str, dict], str]

# Appended as a final user turn once the step cap is hit, with tools removed so
# the model is forced to conclude from what it already gathered.
_FORCE_ANSWER = (
    "You have reached the tool-call limit. Do not request more tools. "
    "Give your best final answer now using the information already gathered."
)


class HarnessUnsupported(RuntimeError):
    """Raised when the loop is handed a provider without native tool-calling.

    The caller is responsible for checking ``supports_native_tools()`` and
    routing to the text-ReAct harness instead; reaching here means a misroute,
    so we fail loudly rather than silently degrade.
    """


@dataclass
class StepRecord:
    """One executed tool call and its observation."""

    index: int
    thought: str
    tool_name: str
    arguments: dict
    observation: str
    error: bool = False


@dataclass
class LoopResult:
    """Outcome of a native tool-calling run.

    ``halted`` is one of ``"answered"`` (model produced a final answer),
    ``"max_steps"`` (step cap hit; ``answer`` is the forced conclusion),
    ``"empty"`` (model returned neither tool calls nor text), or ``"cancelled"``
    (the caller's ``is_cancelled`` fired; ``answer`` is a short notice and
    ``steps`` holds whatever completed first).
    """

    answer: str
    steps: list[StepRecord] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    halted: str = "answered"


_CANCELLED_ANSWER = "(cancelled)"


def run_native_tool_loop(
    *,
    provider: Any,
    question: str,
    tools: list[dict],
    execute_tool: ExecuteTool,
    system: Optional[str] = None,
    max_steps: int = 12,
    max_tokens: Optional[int] = None,
    on_step: Optional[Callable[[StepRecord], None]] = None,
    on_answer: Optional[Callable[[str], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> LoopResult:
    """Drive one native tool-calling run and return a :class:`LoopResult`.

    Raises :class:`HarnessUnsupported` if ``provider`` has no native path, and
    :class:`ValueError` if ``max_steps`` is not positive. A tool that raises is
    not fatal: its error is fed back as the observation so the model can adapt,
    bounded by ``max_steps``. ``is_cancelled`` is polled before each model call
    and after each tool call, so a stopped run exits promptly instead of burning
    the full step budget.
    """
    if not provider.supports_native_tools():
        raise HarnessUnsupported(
            f"{getattr(provider, 'name', 'provider')} has no native tool-calling; "
            "route to the text-ReAct harness instead"
        )
    if max_steps <= 0:
        raise ValueError("max_steps must be positive")

    def _cancelled() -> bool:
        return bool(is_cancelled and is_cancelled())

    messages: list[dict] = [{"role": "user", "content": question}]
    usage = TokenUsage()
    steps: list[StepRecord] = []

    for _ in range(max_steps):
        if _cancelled():
            return LoopResult(answer=_CANCELLED_ANSWER, steps=steps, usage=usage, halted="cancelled")
        turn = provider.generate_with_tools(
            messages, tools, system=system, max_tokens=max_tokens
        )
        usage = usage + (turn.usage or TokenUsage())

        if not turn.tool_calls:
            answer = (turn.text or "").strip()
            halted = "answered" if answer else "empty"
            if on_answer:
                on_answer(answer)
            return LoopResult(answer=answer, steps=steps, usage=usage, halted=halted)

        # Echo the assistant turn (prose + the calls it requested) so the next
        # provider call sees its own tool_use and can be matched to the results.
        messages.append(
            {"role": "assistant", "content": turn.text, "tool_calls": turn.tool_calls}
        )

        for call in turn.tool_calls:
            try:
                observation = execute_tool(call.name, call.arguments)
                errored = False
            except Exception as exc:  # tool failure is recoverable, not fatal
                logger.warning("harness v2: tool %s failed: %s", call.name, exc)
                observation = f"ERROR: {exc}"
                errored = True

            record = StepRecord(
                index=len(steps),
                thought=turn.text or "",
                tool_name=call.name,
                arguments=call.arguments,
                observation=observation,
                error=errored,
            )
            steps.append(record)
            if on_step:
                on_step(record)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": observation,
                }
            )

        if _cancelled():
            return LoopResult(answer=_CANCELLED_ANSWER, steps=steps, usage=usage, halted="cancelled")

    # Step cap reached — force a tool-free conclusion. Drop tools so the model
    # cannot call more, and nudge via the system prompt rather than a second
    # user turn: the message list ends with tool results (a user turn), and
    # appending another user turn would break providers that require strict
    # user/assistant alternation (Anthropic, Gemini).
    force_system = f"{system}\n\n{_FORCE_ANSWER}" if system else _FORCE_ANSWER
    final = provider.generate_with_tools(
        messages,
        [],
        system=force_system,
        max_tokens=max_tokens,
    )
    usage = usage + (final.usage or TokenUsage())
    answer = (final.text or "").strip() or "(no answer; tool-call limit reached)"
    if on_answer:
        on_answer(answer)
    return LoopResult(answer=answer, steps=steps, usage=usage, halted="max_steps")
