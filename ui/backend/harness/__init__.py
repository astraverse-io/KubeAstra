"""Agent Harness v2 — native tool-calling loop (design §6).

Additive to the text-ReAct harness in ``react.py``; gated by the
``AGENT_HARNESS_V2`` setting and used only for providers whose
``supports_native_tools()`` is True. See ``loop.py``.
"""

from .loop import (
    HarnessUnsupported,
    LoopResult,
    StepRecord,
    run_native_tool_loop,
)

__all__ = [
    "HarnessUnsupported",
    "LoopResult",
    "StepRecord",
    "run_native_tool_loop",
]
