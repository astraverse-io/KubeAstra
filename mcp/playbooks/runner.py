"""Playbook runner — executes a playbook's steps and optional LLM synthesis.

Execution flow:
1. Resolve the playbook by name from the registry
2. Validate required params
3. Execute each step in order:
   a. Evaluate condition (skip if falsy)
   b. Template the params with {{ var }} substitution
   c. Dispatch the tool via tool_registry.dispatch()
   d. Store the result under the step's store_as key
4. Optionally run LLM synthesis over the gathered data
5. Return a PlaybookResult with all step outputs
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class StepResult:
    """Result of executing one playbook step."""
    step_index: int
    tool: str
    store_as: str
    params: dict[str, Any]
    result: Optional[dict] = None
    error: Optional[str] = None
    skipped: bool = False
    skip_reason: str = ""
    duration_ms: float = 0.0


@dataclass
class PlaybookResult:
    """Result of executing a complete playbook."""
    playbook_name: str
    success: bool = True
    steps: list[StepResult] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)  # store_as -> result
    synthesis: Optional[str] = None  # LLM synthesis output
    ai_analysis: Optional[dict] = None  # Parsed synthesis JSON
    total_duration_ms: float = 0.0
    error: Optional[str] = None
    params: dict[str, Any] = field(default_factory=dict)


def run_playbook(
    playbook_name: str,
    params: Optional[dict[str, Any]] = None,
    use_ai: bool = True,
) -> PlaybookResult:
    """Execute a playbook by name.

    Args:
        playbook_name: Name of a registered playbook
        params: Runtime parameters (namespace, pod_name, etc.)
        use_ai: Whether to run LLM synthesis after gathering data

    Returns:
        PlaybookResult with step outputs and optional AI synthesis
    """
    from playbooks.registry import get_playbook

    params = params or {}
    start = time.perf_counter()

    playbook = get_playbook(playbook_name)
    if not playbook:
        return PlaybookResult(
            playbook_name=playbook_name,
            success=False,
            error=f"Playbook '{playbook_name}' not found",
        )

    # Validate required params
    for p in playbook.params:
        if p.required and not params.get(p.name):
            return PlaybookResult(
                playbook_name=playbook_name,
                success=False,
                error=f"Missing required parameter: {p.name}",
                params=params,
            )

    # Apply defaults for missing optional params
    context = {}
    for p in playbook.params:
        context[p.name] = params.get(p.name, p.default)

    # Also include any extra params passed at runtime
    for k, v in params.items():
        if k not in context:
            context[k] = v

    result = PlaybookResult(
        playbook_name=playbook_name,
        params=context.copy(),
    )

    # Execute steps in order
    for i, step in enumerate(playbook.steps):
        step_result = _execute_step(i, step, context)
        result.steps.append(step_result)

        if not step_result.skipped and step_result.result is not None:
            result.data[step_result.store_as] = step_result.result
            # Make result available for templating in subsequent steps
            context[step_result.store_as] = step_result.result

        if step_result.error and step.on_error == "abort":
            result.success = False
            result.error = f"Step {i + 1} ({step.tool}) failed: {step_result.error}"
            break

    # LLM synthesis
    if use_ai and playbook.synthesis_prompt and result.success:
        synthesis_result = _run_synthesis(playbook, context, result.data)
        result.synthesis = synthesis_result.get("raw")
        result.ai_analysis = synthesis_result.get("parsed")

    result.total_duration_ms = (time.perf_counter() - start) * 1000

    logger.info(
        "playbook_complete name=%s steps=%d/%d duration_ms=%.1f success=%s",
        playbook_name,
        len([s for s in result.steps if not s.skipped]),
        len(playbook.steps),
        result.total_duration_ms,
        result.success,
    )

    return result


def _execute_step(
    index: int,
    step: "PlaybookStep",
    context: dict[str, Any],
) -> StepResult:
    """Execute a single playbook step."""
    from playbooks.schema import PlaybookStep
    from tool_registry import DispatchContext, dispatch

    # Check condition
    if step.condition:
        if not _evaluate_condition(step.condition, context):
            return StepResult(
                step_index=index,
                tool=step.tool,
                store_as=step.store_as,
                params={},
                skipped=True,
                skip_reason=f"Condition not met: {step.condition}",
            )

    # Template the params
    templated_params = _template_params(step.params, context)

    start = time.perf_counter()
    try:
        ctx = DispatchContext(surface="playbook")
        tool_result = dispatch(step.tool, templated_params, ctx)
        duration = (time.perf_counter() - start) * 1000

        return StepResult(
            step_index=index,
            tool=step.tool,
            store_as=step.store_as,
            params=templated_params,
            result=tool_result,
            duration_ms=duration,
        )
    except Exception as e:
        duration = (time.perf_counter() - start) * 1000
        logger.warning("Playbook step %d (%s) failed: %s", index, step.tool, e)
        return StepResult(
            step_index=index,
            tool=step.tool,
            store_as=step.store_as,
            params=templated_params,
            error=str(e),
            duration_ms=duration,
        )


def _template_params(
    params: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Replace {{ var }} placeholders in param values with context values.

    Supports simple variable substitution only — not full Jinja2.
    Nested dicts/lists are recursively templated.
    """
    result = {}
    for key, value in params.items():
        if isinstance(value, str):
            result[key] = _template_string(value, context)
        elif isinstance(value, dict):
            result[key] = _template_params(value, context)
        elif isinstance(value, list):
            result[key] = [
                _template_string(v, context) if isinstance(v, str) else v
                for v in value
            ]
        else:
            result[key] = value
    return result


def _template_string(text: str, context: dict[str, Any]) -> Any:
    """Replace {{ var }} in a string with context values.

    If the entire string is a single {{ var }} reference and the context
    value is not a string (e.g. a dict or bool), return the raw value
    instead of stringifying it.
    """
    # Check if the whole string is a single variable reference
    single_match = re.fullmatch(r"\{\{\s*(\w+)\s*\}\}", text)
    if single_match:
        var_name = single_match.group(1)
        return context.get(var_name, text)

    # Otherwise, do string interpolation for mixed content
    def _replace(match):
        var_name = match.group(1).strip()
        value = context.get(var_name, match.group(0))
        if isinstance(value, (dict, list)):
            return json.dumps(value, default=str)[:500]
        return str(value)

    return re.sub(r"\{\{\s*(\w+)\s*\}\}", _replace, text)


def _evaluate_condition(condition: str, context: dict[str, Any]) -> bool:
    """Evaluate a simple {{ var == 'value' }} or {{ var }} condition.

    Supports:
      - {{ var }}           — truthy check
      - {{ var == 'value' }} — equality
      - {{ var != 'value' }} — inequality
      - {{ var in 'text' }}  — substring check
    """
    # Extract the expression from {{ ... }}
    match = re.search(r"\{\{\s*(.+?)\s*\}\}", condition)
    if not match:
        return bool(condition.strip())

    expr = match.group(1).strip()

    # Equality check: var == 'value'
    eq_match = re.match(r"(\w+)\s*==\s*['\"](.+?)['\"]", expr)
    if eq_match:
        var_name, expected = eq_match.groups()
        return str(context.get(var_name, "")) == expected

    # Inequality check: var != 'value'
    neq_match = re.match(r"(\w+)\s*!=\s*['\"](.+?)['\"]", expr)
    if neq_match:
        var_name, expected = neq_match.groups()
        return str(context.get(var_name, "")) != expected

    # Substring check: var in 'text'
    in_match = re.match(r"(\w+)\s+in\s+['\"](.+?)['\"]", expr)
    if in_match:
        var_name, haystack = in_match.groups()
        return str(context.get(var_name, "")) in haystack

    # Simple truthy check: {{ var }}
    return bool(context.get(expr, ""))


def _run_synthesis(
    playbook: "Playbook",
    context: dict[str, Any],
    data: dict[str, Any],
) -> dict[str, Any]:
    """Run LLM synthesis over the gathered playbook data."""
    try:
        from services.llm_service import llm_service
    except ImportError:
        logger.warning("LLM service not available, skipping synthesis")
        return {}

    if not llm_service.provider.enabled:
        return {}

    # Build the synthesis prompt with data
    prompt = _template_string(playbook.synthesis_prompt, {**context, **data})

    # Truncate large data in the prompt
    if len(prompt) > 8000:
        prompt = prompt[:8000] + "\n\n(truncated for context window)"

    system = (
        "You are Kubeastra, an expert Kubernetes troubleshooting agent. "
        "You are analyzing the results of an automated investigation playbook. "
        "Respond ONLY with valid JSON matching this schema:\n"
        "{\n"
        '  "root_cause": "One sentence root cause",\n'
        '  "solution": "How to fix it",\n'
        '  "steps": ["Step 1", "Step 2"],\n'
        '  "commands": [{"cmd": "kubectl ...", "description": "What this does"}],\n'
        '  "prevention": "How to prevent this",\n'
        '  "severity": "critical|high|medium|low",\n'
        '  "confidence": 0.95,\n'
        '  "category": "pod_crashloop"\n'
        "}"
    )

    try:
        raw = llm_service.provider.generate(prompt, system=system, temperature=0.2)
        parsed = _parse_synthesis(raw)
        return {"raw": raw, "parsed": parsed}
    except Exception as e:
        logger.warning("Playbook synthesis failed: %s", e)
        return {"raw": None, "parsed": None}


def _parse_synthesis(text: str) -> Optional[dict]:
    """Parse LLM synthesis JSON, handling markdown fences."""
    if not text:
        return None

    cleaned = text.strip()
    if "```" in cleaned:
        for part in cleaned.split("```"):
            part = part.strip().lstrip("json").strip()
            if part.startswith("{"):
                cleaned = part
                break

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None
