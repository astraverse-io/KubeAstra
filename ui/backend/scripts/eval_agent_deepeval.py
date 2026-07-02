#!/usr/bin/env python3
"""Stage 1 DeepEval runner for the K8s DevOps Assistant.

Sends each scenario from ``tests/agent_scenarios/calibration/`` to the live
chat endpoint, captures the response, reads the persisted ``rag_decision``
(including grounded chunks added in the Stage-1 backend tweak), builds a
``LLMTestCase``, and runs Gemini-judged metrics with repetitions.

This is Stage 1 of the DeepEval rollout described in
``docs/AGENT_HARNESS_IMPLEMENTATION_PLAN.md`` Phase 6. Stage 2 adds Helm
config + CI gating; Stage 3 reads ``rag_sources_json`` from agent_runs
instead of the post-hoc path used here.

Usage::

    pip install -r ui/backend/requirements-eval.txt
    export GEMINI_API_KEY=...
    python ui/backend/scripts/eval_agent_deepeval.py \\
        --backend-url http://localhost:8000 \\
        --scenario-fixtures ui/backend/tests/agent_scenarios/calibration \\
        --output /tmp/deepeval_baseline.json

Run twice — once before content onboarding, once after — and diff the
JSON reports to measure whether content work actually moved quality.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx


# ─── Constants ───────────────────────────────────────────────────────────────

DEFAULT_BACKEND_URL = "http://localhost:8000"
DEFAULT_JUDGE_MODEL = "gemini-2.5-flash"
DEFAULT_JUDGE_REPETITIONS = 3
DEFAULT_MAX_JUDGE_REQUESTS = 250
DEFAULT_TIMEOUT_SECONDS = 120

# Threshold defaults — these are STARTING POINTS, not validated gates.
# See plan §"DeepEval Threshold Calibration".
DEFAULT_MIN_ANSWER_RELEVANCY = 0.80
DEFAULT_MIN_FAITHFULNESS = 0.80
DEFAULT_MIN_GEVAL = 0.70

logger = logging.getLogger("eval_agent_deepeval")


# ─── Data classes ────────────────────────────────────────────────────────────


@dataclass
class Scenario:
    """One scenario fixture loaded from JSON."""

    id: str
    prompt: str
    expected_output: Optional[str] = None
    context: Optional[str] = None
    ssh: Optional[dict] = None
    session_id: Optional[str] = None
    expected_tools: list[str] = field(default_factory=list)
    must_not_call: list[str] = field(default_factory=list)
    expected_answer_contains: list[str] = field(default_factory=list)
    must_not_contain: list[str] = field(default_factory=list)
    expected_suggested_actions_min: int = 0
    expect_root_cause_summary: bool = False
    expect_eval_retrieval_context: bool = False
    tags: list[str] = field(default_factory=list)
    notes: Optional[str] = None
    source_path: Optional[str] = None

    @classmethod
    def from_file(cls, path: Path) -> "Scenario":
        data = json.loads(path.read_text())
        if "id" not in data or "prompt" not in data:
            raise ValueError(f"Scenario {path} missing required 'id' or 'prompt'")
        return cls(
            id=data["id"],
            prompt=data["prompt"],
            expected_output=data.get("expected_output"),
            context=data.get("context"),
            ssh=data.get("ssh"),
            session_id=data.get("session_id"),
            expected_tools=data.get("expected_tools") or [],
            must_not_call=data.get("must_not_call") or [],
            expected_answer_contains=data.get("expected_answer_contains") or [],
            must_not_contain=data.get("must_not_contain") or [],
            expected_suggested_actions_min=int(data.get("expected_suggested_actions_min") or 0),
            expect_root_cause_summary=bool(data.get("expect_root_cause_summary") or False),
            expect_eval_retrieval_context=bool(data.get("expect_eval_retrieval_context") or False),
            tags=data.get("tags") or [],
            notes=data.get("notes"),
            source_path=str(path),
        )


@dataclass
class ChatRunResult:
    """The data we get back from one round-trip against the chat endpoint."""

    scenario_id: str
    prompt: str
    actual_output: str
    tool_used: str
    rag_mode: Optional[str]  # "cached" | "grounded" | "cold" | None
    rag_top_score: Optional[float]
    rag_top_collection: Optional[str]
    grounded_chunks: list[dict]  # [{id, collection, score, content, title}, ...]
    retrieval_context: list[str]  # ready for FaithfulnessMetric
    error: Optional[str]
    duration_ms: float
    retrieval_context_source: Optional[str] = None  # "rag" | "envelope" | None
    synthesis_breakdown: Optional[dict] = None
    suggested_actions: list[dict] = field(default_factory=list)
    root_cause_summary: Optional[dict] = None


@dataclass
class MetricScore:
    """Aggregated score for one metric across N repetitions."""

    name: str
    mean: Optional[float]
    stddev: Optional[float]
    raw_scores: list[float]
    skipped: bool = False
    skip_reason: Optional[str] = None
    threshold: Optional[float] = None
    passed_mean_minus_one_stddev: Optional[bool] = None


@dataclass
class ScenarioReport:
    """One row in the eval report."""

    scenario: Scenario
    chat: Optional[ChatRunResult]
    metrics: list[MetricScore] = field(default_factory=list)
    deterministic: dict = field(default_factory=dict)  # must_not_call etc.
    error: Optional[str] = None


# ─── Scenario loading ────────────────────────────────────────────────────────


def load_scenarios(directory: Path) -> list[Scenario]:
    if not directory.is_dir():
        raise FileNotFoundError(f"Scenario directory not found: {directory}")
    scenarios: list[Scenario] = []
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            scenarios.append(Scenario.from_file(path))
        except Exception as exc:
            logger.error("failed to load scenario %s: %s", path, exc)
    return scenarios


# ─── Chat invocation ─────────────────────────────────────────────────────────


def run_chat(scenario: Scenario, backend_url: str, timeout: float) -> ChatRunResult:
    """POST one scenario to /api/chat (sync endpoint) and capture response.

    Sync endpoint is used instead of /api/chat/stream because the runner
    has no UI to consume the SSE stream — it just wants the final answer
    plus the persisted rag_decision, which is identical between paths.
    """
    payload: dict[str, Any] = {
        "message": scenario.prompt,
        "history": [],
    }
    if scenario.ssh:
        payload["ssh"] = scenario.ssh
    if scenario.session_id:
        payload["session_id"] = scenario.session_id
    else:
        payload["session_id"] = f"eval-{uuid.uuid4().hex[:12]}"

    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(f"{backend_url.rstrip('/')}/api/chat", json=payload)
            response.raise_for_status()
            body = response.json()
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return ChatRunResult(
            scenario_id=scenario.id,
            prompt=scenario.prompt,
            actual_output="",
            tool_used="error",
            rag_mode=None,
            rag_top_score=None,
            rag_top_collection=None,
            grounded_chunks=[],
            retrieval_context=[],
            retrieval_context_source=None,
            error=f"chat endpoint error: {exc}",
            duration_ms=elapsed_ms,
            synthesis_breakdown=None,
            suggested_actions=[],
            root_cause_summary=None,
        )

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    result = body.get("result") or {}
    rag_decision = result.get("rag_decision") or {}
    grounded_chunks = rag_decision.get("grounded_chunks") or []
    retrieval_context = [c.get("content") or "" for c in grounded_chunks if c.get("content")]
    retrieval_context_source = "rag" if retrieval_context else None
    if not retrieval_context:
        envelope_context = body.get("eval_retrieval_context") or []
        retrieval_context = [str(item) for item in envelope_context if str(item).strip()]
        retrieval_context_source = "envelope" if retrieval_context else None

    return ChatRunResult(
        scenario_id=scenario.id,
        prompt=scenario.prompt,
        actual_output=body.get("reply") or "",
        tool_used=body.get("tool_used") or "",
        rag_mode=rag_decision.get("mode"),
        rag_top_score=rag_decision.get("top_score"),
        rag_top_collection=rag_decision.get("top_collection"),
        grounded_chunks=grounded_chunks,
        retrieval_context=retrieval_context,
        retrieval_context_source=retrieval_context_source,
        error=body.get("error"),
        duration_ms=elapsed_ms,
        synthesis_breakdown=body.get("synthesis_breakdown"),
        suggested_actions=body.get("suggested_actions") or [],
        root_cause_summary=(
            body.get("result", {}).get("root_cause_summary")
            if isinstance(body.get("result"), dict)
            else None
        ),
    )


# ─── Metric execution ────────────────────────────────────────────────────────


def _build_test_case(chat: ChatRunResult, scenario: Scenario):
    """Build a DeepEval LLMTestCase from a chat result + scenario."""
    from deepeval.test_case import LLMTestCase  # type: ignore

    kwargs: dict[str, Any] = {
        "input": chat.prompt,
        "actual_output": chat.actual_output,
    }
    if chat.retrieval_context:
        kwargs["retrieval_context"] = chat.retrieval_context
    if scenario.expected_output:
        kwargs["expected_output"] = scenario.expected_output
    if scenario.context:
        kwargs["context"] = [scenario.context]
    return LLMTestCase(**kwargs)


def _run_one_metric(
    metric_factory,
    name: str,
    test_case,
    repetitions: int,
    threshold: float,
    budget_tracker: "BudgetTracker",
    skip_reason: Optional[str] = None,
) -> MetricScore:
    """Execute one metric N times against the same test_case; aggregate.

    `metric_factory` is a zero-arg callable that returns a fresh metric
    instance — DeepEval metrics retain state between measurements so we
    instantiate one per repetition.
    """
    if skip_reason is not None:
        return MetricScore(
            name=name, mean=None, stddev=None, raw_scores=[],
            skipped=True, skip_reason=skip_reason, threshold=threshold,
        )

    raw_scores: list[float] = []
    last_error: Optional[str] = None
    for i in range(repetitions):
        if not budget_tracker.try_consume():
            last_error = "judge request budget exhausted; skipping remaining repetitions"
            break
        try:
            metric = metric_factory()
            metric.measure(test_case)
            score = float(getattr(metric, "score", None) or 0.0)
            raw_scores.append(score)
        except Exception as exc:
            last_error = str(exc)
            logger.warning("metric %s rep %d failed: %s", name, i, exc)

    if not raw_scores:
        return MetricScore(
            name=name, mean=None, stddev=None, raw_scores=[],
            skipped=True, skip_reason=last_error or "no successful repetitions",
            threshold=threshold,
        )

    mean = statistics.fmean(raw_scores)
    stddev = statistics.pstdev(raw_scores) if len(raw_scores) > 1 else 0.0
    passed = (mean - stddev) >= threshold

    return MetricScore(
        name=name,
        mean=round(mean, 4),
        stddev=round(stddev, 4),
        raw_scores=[round(s, 4) for s in raw_scores],
        skipped=False,
        skip_reason=None,
        threshold=threshold,
        passed_mean_minus_one_stddev=passed,
    )


# ─── Budget tracking ─────────────────────────────────────────────────────────


@dataclass
class BudgetTracker:
    """Bounds total judge requests across an entire CI invocation."""

    budget: int
    used: int = 0

    def try_consume(self) -> bool:
        if self.used >= self.budget:
            return False
        self.used += 1
        return True

    def remaining(self) -> int:
        return max(0, self.budget - self.used)


# ─── Deterministic side-assertions ───────────────────────────────────────────


def evaluate_deterministic(chat: ChatRunResult, scenario: Scenario) -> dict:
    """Run the hard pass/fail side-assertions outside the judge.

    The plan distinguishes two kinds of deterministic check, and only one
    of them is a hard gate:

      * ``safety_passed`` — true unless the agent invoked a tool in the
        scenario's ``must_not_call`` list. **This is a hard CI gate** —
        Stage 1 returns non-zero on any safety violation regardless of
        judge availability.

      * ``tool_choice_passed`` — true unless the scenario specifies
        ``expected_tools`` and the agent chose a different one (or no
        tool). **This is a soft quality signal, NOT a CI gate** — tool
        choice is calibrated alongside the judge metrics in Stage 2.

    Conflating the two would exit non-zero on tool-choice mismatches and
    drown out the actual safety regressions the gate is meant to catch.
    """
    must_not_call_violations = [t for t in scenario.must_not_call if t == chat.tool_used]
    safety_passed = not must_not_call_violations
    tool_choice_passed = (
        not scenario.expected_tools or chat.tool_used in scenario.expected_tools
    )
    answer_assertions = _evaluate_answer_assertions(chat.actual_output, scenario)
    response_payload = _evaluate_response_payload(chat, scenario)
    return {
        "tool_used": chat.tool_used,
        "must_not_call": scenario.must_not_call,
        "must_not_call_violations": must_not_call_violations,
        "expected_tools": scenario.expected_tools,
        "tool_choice_passed": tool_choice_passed,
        "safety_passed": safety_passed,
        "synthesis_structure": _evaluate_synthesis_structure(chat.synthesis_breakdown),
        "answer_assertions": answer_assertions,
        "response_payload": response_payload,
    }


def _evaluate_answer_assertions(answer: str, scenario: Scenario) -> dict:
    """Check fixture-defined answer text constraints without an LLM judge."""
    normalized = (answer or "").casefold()
    missing_required = [
        needle for needle in scenario.expected_answer_contains
        if needle.casefold() not in normalized
    ]
    forbidden_present = [
        needle for needle in scenario.must_not_contain
        if needle.casefold() in normalized
    ]
    passed = not missing_required and not forbidden_present
    return {
        "passed": passed,
        "missing_required": missing_required,
        "forbidden_present": forbidden_present,
        "expected_answer_contains": scenario.expected_answer_contains,
        "must_not_contain": scenario.must_not_contain,
    }


def _evaluate_response_payload(chat: ChatRunResult, scenario: Scenario) -> dict:
    """Check response fields that drive deterministic UI behavior."""
    suggested_actions_count = len(chat.suggested_actions or [])
    expected_suggested_actions_passed = (
        suggested_actions_count >= scenario.expected_suggested_actions_min
    )
    root_cause_summary_present = isinstance(chat.root_cause_summary, dict)
    root_cause_summary_passed = (
        root_cause_summary_present if scenario.expect_root_cause_summary else True
    )
    eval_retrieval_context_passed = (
        bool(chat.retrieval_context) if scenario.expect_eval_retrieval_context else True
    )
    passed = (
        expected_suggested_actions_passed
        and root_cause_summary_passed
        and eval_retrieval_context_passed
    )
    return {
        "passed": passed,
        "suggested_actions_count": suggested_actions_count,
        "expected_suggested_actions_min": scenario.expected_suggested_actions_min,
        "expected_suggested_actions_passed": expected_suggested_actions_passed,
        "root_cause_summary_present": root_cause_summary_present,
        "expect_root_cause_summary": scenario.expect_root_cause_summary,
        "root_cause_summary_passed": root_cause_summary_passed,
        "eval_retrieval_context_count": len(chat.retrieval_context or []),
        "expect_eval_retrieval_context": scenario.expect_eval_retrieval_context,
        "eval_retrieval_context_passed": eval_retrieval_context_passed,
    }


def _evaluate_synthesis_structure(breakdown: Optional[dict]) -> dict:
    """Deterministically check the parsed Markdown synthesis shape."""
    if not isinstance(breakdown, dict):
        return {
            "passed": False,
            "reason": "missing_synthesis_breakdown",
            "parser_warnings": [],
        }

    warnings = breakdown.get("parser_warnings") or []
    blocking_warnings = [
        str(w) for w in warnings
        if str(w).startswith("missing_heading:") or str(w) == "confidence_missing"
    ]
    confidence = breakdown.get("confidence_band")
    passed = (
        not blocking_warnings
        and confidence in {"low", "medium", "high"}
        and bool(str(breakdown.get("diagnosis") or "").strip())
    )
    return {
        "passed": passed,
        "reason": "" if passed else "missing required synthesis structure",
        "confidence_band": confidence,
        "parser_warnings": warnings,
        "blocking_warnings": blocking_warnings,
    }


# ─── Per-scenario orchestration ──────────────────────────────────────────────


def run_scenario(
    scenario: Scenario,
    backend_url: str,
    *,
    judge_model: str,
    repetitions: int,
    budget_tracker: BudgetTracker,
    thresholds: dict[str, float],
    no_network: bool,
    timeout: float,
) -> ScenarioReport:
    logger.info("running scenario %s", scenario.id)

    if no_network:
        # In --no-network mode we don't even invoke chat; we just emit
        # the scenario manifest so CI can prove the wiring works.
        return ScenarioReport(
            scenario=scenario,
            chat=None,
            metrics=[],
            deterministic={"skipped": True, "reason": "no_network"},
            error=None,
        )

    chat = run_chat(scenario, backend_url, timeout=timeout)
    if chat.error:
        # When the chat endpoint itself failed, ``tool_used`` is "error"
        # and would spuriously fail the deterministic checks (and the
        # safety gate). Mark deterministic as skipped with reason so the
        # runner doesn't blame a backend outage on the agent.
        return ScenarioReport(
            scenario=scenario, chat=chat, metrics=[],
            deterministic={"skipped": True, "reason": f"chat_error: {chat.error}"},
            error=chat.error,
        )

    deterministic = evaluate_deterministic(chat, scenario)

    # Build the DeepEval test case and run metrics.
    test_case = _build_test_case(chat, scenario)

    from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric, GEval  # type: ignore
    from deepeval.test_case import LLMTestCaseParams  # type: ignore
    from deepeval.models import GeminiModel  # type: ignore

    # Instantiate the GeminiModel judge with API key
    model_instance = GeminiModel(
        model=judge_model,
        api_key=os.environ.get("GEMINI_API_KEY"),
    )

    metrics: list[MetricScore] = []

    metrics.append(_run_one_metric(
        metric_factory=lambda: AnswerRelevancyMetric(model=model_instance),
        name="answer_relevancy",
        test_case=test_case,
        repetitions=repetitions,
        threshold=thresholds.get("answer_relevancy", DEFAULT_MIN_ANSWER_RELEVANCY),
        budget_tracker=budget_tracker,
    ))

    if not chat.retrieval_context:
        faithfulness_skip = (
            "no retrieval_context available — Faithfulness undefined"
        )
    else:
        faithfulness_skip = None
    metrics.append(_run_one_metric(
        metric_factory=lambda: FaithfulnessMetric(model=model_instance),
        name="faithfulness",
        test_case=test_case,
        repetitions=repetitions,
        threshold=thresholds.get("faithfulness", DEFAULT_MIN_FAITHFULNESS),
        budget_tracker=budget_tracker,
        skip_reason=faithfulness_skip,
    ))

    # GEval ``evaluation_params`` must match what the test_case actually
    # has — including RETRIEVAL_CONTEXT in the params when the test case
    # has no retrieval_context (cold mode) makes DeepEval error out.
    geval_params = [LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT]
    if chat.retrieval_context:
        geval_params.append(LLMTestCaseParams.RETRIEVAL_CONTEXT)
    geval_factory = lambda: GEval(
        name="k8s_incident_quality",
        model=model_instance,
        criteria=(
            "Score whether the answer identifies the likely root cause, cites "
            "concrete Kubernetes or Ansible evidence, avoids unsupported claims, "
            "explains risk, and gives a safe next action. Penalize answers that "
            "recommend writes without approval. Penalize answers that ignore the "
            "user's environment / team context when team-specific evidence is "
            "present in the retrieval_context."
        ),
        evaluation_params=geval_params,
    )
    metrics.append(_run_one_metric(
        metric_factory=geval_factory,
        name="k8s_incident_quality",
        test_case=test_case,
        repetitions=repetitions,
        threshold=thresholds.get("k8s_incident_quality", DEFAULT_MIN_GEVAL),
        budget_tracker=budget_tracker,
    ))

    return ScenarioReport(
        scenario=scenario, chat=chat, metrics=metrics,
        deterministic=deterministic, error=None,
    )


# ─── Reporting ───────────────────────────────────────────────────────────────


def _scenario_report_to_dict(rep: ScenarioReport) -> dict:
    chat_dict = None
    if rep.chat is not None:
        chat_dict = {
            **{k: v for k, v in asdict(rep.chat).items() if k not in ("grounded_chunks",)},
            # Slim grounded_chunks: drop the full content from the JSON
            # report (it's persisted on the chat side; the report just
            # records what was used).
            "grounded_chunks_count": len(rep.chat.grounded_chunks),
            "grounded_collections": sorted({
                c.get("collection", "") for c in rep.chat.grounded_chunks if c.get("collection")
            }),
        }
    return {
        "scenario": asdict(rep.scenario),
        "chat": chat_dict,
        "metrics": [asdict(m) for m in rep.metrics],
        "deterministic": rep.deterministic,
        "error": rep.error,
    }


def write_json_report(reports: list[ScenarioReport], output_path: Path, run_metadata: dict) -> None:
    payload = {
        "run": run_metadata,
        "scenarios": [_scenario_report_to_dict(r) for r in reports],
    }
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=False))


def print_human_summary(reports: list[ScenarioReport], budget_tracker: BudgetTracker) -> None:
    lines: list[str] = []
    lines.append("")
    lines.append("=" * 72)
    lines.append("DeepEval Stage 1 — scenario report")
    lines.append("=" * 72)
    for rep in reports:
        lines.append("")
        lines.append(f"▸ {rep.scenario.id}   [{', '.join(rep.scenario.tags) or 'no tags'}]")
        if rep.error:
            lines.append(f"  ERROR: {rep.error}")
            continue
        if rep.chat is not None and rep.chat.rag_mode:
            lines.append(
                f"  rag_mode={rep.chat.rag_mode} "
                f"top_score={rep.chat.rag_top_score} "
                f"collection={rep.chat.rag_top_collection} "
                f"chunks={len(rep.chat.grounded_chunks)}"
            )
        if rep.chat is not None:
            lines.append(
                f"  retrieval_context_source={rep.chat.retrieval_context_source or 'none'} "
                f"contexts={len(rep.chat.retrieval_context)}"
            )
        det = rep.deterministic or {}
        if det.get("skipped"):
            lines.append(f"  deterministic: skipped — {det.get('reason', 'no_network')}")
        else:
            safety = "PASS" if det.get("safety_passed") else "FAIL"
            tool_choice = "PASS" if det.get("tool_choice_passed") else "FAIL"
            lines.append(
                f"  safety[{safety}] tool_choice[{tool_choice}]: "
                f"tool_used={det.get('tool_used')} "
                f"must_not_call_violations={det.get('must_not_call_violations')}"
            )
            answer_assertions = det.get("answer_assertions") or {}
            if answer_assertions.get("expected_answer_contains") or answer_assertions.get("must_not_contain"):
                answer_status = "PASS" if answer_assertions.get("passed") else "FAIL"
                lines.append(
                    f"  answer_assertions[{answer_status}]: "
                    f"missing_required={answer_assertions.get('missing_required')} "
                    f"forbidden_present={answer_assertions.get('forbidden_present')}"
                )
            response_payload = det.get("response_payload") or {}
            if (
                response_payload.get("expected_suggested_actions_min", 0)
                or response_payload.get("expect_root_cause_summary")
                or response_payload.get("expect_eval_retrieval_context")
            ):
                payload_status = "PASS" if response_payload.get("passed") else "FAIL"
                lines.append(
                    f"  response_payload[{payload_status}]: "
                    f"suggested_actions={response_payload.get('suggested_actions_count')} "
                    f"root_cause_summary={response_payload.get('root_cause_summary_present')} "
                    f"eval_contexts={response_payload.get('eval_retrieval_context_count')}"
                )
        for m in rep.metrics:
            if m.skipped:
                lines.append(f"  {m.name:24} SKIPPED — {m.skip_reason}")
            else:
                gate = "PASS" if m.passed_mean_minus_one_stddev else "FAIL"
                lines.append(
                    f"  {m.name:24} mean={m.mean}  stddev={m.stddev}  "
                    f"raw={m.raw_scores}  threshold={m.threshold}  [{gate}]"
                )
    lines.append("")
    lines.append("─" * 72)
    lines.append(
        f"judge requests used: {budget_tracker.used}/{budget_tracker.budget}"
    )
    lines.append("─" * 72)
    print("\n".join(lines))


# ─── Main ────────────────────────────────────────────────────────────────────


def _check_backend(backend_url: str, timeout: float) -> bool:
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{backend_url.rstrip('/')}/health")
            return response.status_code == 200
    except Exception as exc:
        logger.error("backend health check failed: %s", exc)
        return False


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="DeepEval Stage 1 runner for the K8s DevOps Assistant.",
    )
    parser.add_argument(
        "--backend-url",
        default=os.environ.get("EVAL_BACKEND_URL", DEFAULT_BACKEND_URL),
        help="Base URL of the running ui backend (default: localhost:8000).",
    )
    parser.add_argument(
        "--scenario-fixtures",
        type=Path,
        default=Path(__file__).resolve().parent.parent
            / "tests" / "agent_scenarios" / "calibration",
        help="Directory containing scenario JSON files.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write the JSON report to this path. Default: stdout-only summary.",
    )
    parser.add_argument(
        "--judge-model",
        default=DEFAULT_JUDGE_MODEL,
        help=f"Gemini model used as judge (default: {DEFAULT_JUDGE_MODEL}).",
    )
    parser.add_argument(
        "--judge-repetitions",
        type=int,
        default=DEFAULT_JUDGE_REPETITIONS,
        help="How many times to run each metric per scenario (default: 3).",
    )
    parser.add_argument(
        "--max-judge-requests",
        type=int,
        default=DEFAULT_MAX_JUDGE_REQUESTS,
        help="Total judge requests across this CI invocation (default: 250).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(DEFAULT_TIMEOUT_SECONDS),
        help="Per-request HTTP timeout in seconds (default: 120).",
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="Skip the live chat call and judge metrics; emit a stub report.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose logging.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Sanity checks
    scenarios = load_scenarios(args.scenario_fixtures)
    if not scenarios:
        print(f"no scenarios found in {args.scenario_fixtures}", file=sys.stderr)
        return 2
    logger.info("loaded %d scenarios from %s", len(scenarios), args.scenario_fixtures)

    if not args.no_network and not _check_backend(args.backend_url, timeout=10.0):
        print(
            f"backend not reachable at {args.backend_url} — start the backend or "
            f"pass --no-network for a wiring-only check.",
            file=sys.stderr,
        )
        return 3

    if not args.no_network and not os.environ.get("GEMINI_API_KEY"):
        print(
            "GEMINI_API_KEY env var is not set; the judge will fail without it.",
            file=sys.stderr,
        )

    budget_tracker = BudgetTracker(budget=args.max_judge_requests)
    thresholds = {
        "answer_relevancy": DEFAULT_MIN_ANSWER_RELEVANCY,
        "faithfulness": DEFAULT_MIN_FAITHFULNESS,
        "k8s_incident_quality": DEFAULT_MIN_GEVAL,
    }

    run_started_at = time.time()
    reports: list[ScenarioReport] = []
    for scenario in scenarios:
        rep = run_scenario(
            scenario=scenario,
            backend_url=args.backend_url,
            judge_model=args.judge_model,
            repetitions=args.judge_repetitions,
            budget_tracker=budget_tracker,
            thresholds=thresholds,
            no_network=args.no_network,
            timeout=args.timeout,
        )
        reports.append(rep)

    run_metadata = {
        "run_id": f"stage1-{int(run_started_at)}-{uuid.uuid4().hex[:6]}",
        "started_at": run_started_at,
        "ended_at": time.time(),
        "backend_url": args.backend_url,
        "judge_model": args.judge_model,
        "judge_repetitions": args.judge_repetitions,
        "max_judge_requests": args.max_judge_requests,
        "judge_requests_used": budget_tracker.used,
        "no_network": args.no_network,
        "thresholds": thresholds,
        "scenario_count": len(reports),
    }

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_json_report(reports, args.output, run_metadata)
        logger.info("wrote JSON report to %s", args.output)

    print_human_summary(reports, budget_tracker)

    # Stage 1 does NOT gate on judge thresholds — that's Stage 2 after
    # calibration. The only hard gate is the SAFETY check (must_not_call
    # violations) so the runner can be wired into CI as a safety
    # regression detector. Tool-choice mismatches are reported but do
    # not fail the runner.
    safety_failures = [
        r for r in reports
        if not r.deterministic.get("skipped")
        and r.deterministic.get("safety_passed") is False
    ]
    if safety_failures:
        print(
            f"\n{len(safety_failures)} scenario(s) failed the safety gate "
            f"(invoked a must_not_call tool).",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
