"""Reusable eval-runner scaffold (blueprint Appendix E).

Each feature's `evals/<feature>/run.py` script imports `run_eval()` from
here, points it at a YAML test set + scoring function, and prints results.

Design notes:
  - YAML test sets live next to each runner so they version with the code.
  - Scoring functions return a float in [0, 1]; 1.0 = perfect, 0.0 = fail.
  - Pass criterion is an aggregate threshold (e.g. 95% of cases score ≥ 0.8).
  - LIVE_LLM=true switches on real-provider calls; default uses mocks/fakes.
  - Failed cases print to stderr with name + score + actual output for triage.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional

import yaml


# ── LIVE vs MOCKED switch ────────────────────────────────────────────────────

def is_live() -> bool:
    """True when evals should hit real LLM/Qdrant. Default: false (CI).

    Set EVAL_LIVE_LLM=true to flip — typically only on main-branch nightly
    runs that have provider credentials in the env.
    """
    return os.environ.get("EVAL_LIVE_LLM", "").lower() in ("true", "1", "yes")


# ── Core runner ──────────────────────────────────────────────────────────────

def run_eval(
    test_file: str,
    fn_under_test: Callable[[dict], Any],
    scorer: Callable[[dict, Any], float],
    pass_threshold: float = 1.0,
    description: str = "",
) -> int:
    """Load `test_file`, run each case through `fn_under_test`, score, report.

    Args:
        test_file: Path to a YAML file containing a list of test cases.
                   Each case is a dict; shape is feature-specific.
        fn_under_test: Function that takes one case dict and returns the
                       actual output (e.g. router decision, summarizer result).
        scorer: Function (case, actual) → score in [0, 1].
        pass_threshold: Fraction of cases that must score >= 1.0.
                        Use 1.0 for security-critical features, 0.8-0.9 for
                        quality-of-results features (where some drift is OK).
        description: Human-readable name shown in the output.

    Returns:
        Exit code: 0 on pass, 1 on fail.
    """
    test_path = Path(test_file)
    if not test_path.exists():
        sys.stderr.write(f"[eval] test set not found: {test_file}\n")
        return 1

    cases = yaml.safe_load(test_path.read_text(encoding="utf-8")) or []
    if not isinstance(cases, list):
        sys.stderr.write(f"[eval] test set must be a YAML list, got {type(cases).__name__}\n")
        return 1

    results: list[tuple[str, float, Any]] = []
    for case in cases:
        name = case.get("name", "?")
        try:
            actual = fn_under_test(case)
        except Exception as exc:
            results.append((name, 0.0, {"error": f"{type(exc).__name__}: {exc}"}))
            continue
        try:
            score = float(scorer(case, actual))
        except Exception as exc:
            results.append((name, 0.0, {"score_error": f"{type(exc).__name__}: {exc}",
                                         "actual": actual}))
            continue
        results.append((name, score, actual))

    passed = sum(1 for _, s, _ in results if s >= 1.0)
    total = len(results)
    pass_rate = passed / total if total else 0.0

    label = description or test_path.stem
    mode = "LIVE" if is_live() else "MOCKED"
    print(f"[eval/{mode}] {label}: {passed}/{total} pass ({pass_rate:.0%})")

    if pass_rate < pass_threshold:
        # Print failing cases to stderr so CI logs flag them.
        sys.stderr.write(f"[eval] {label}: BELOW THRESHOLD ({pass_threshold:.0%})\n")
        for name, score, actual in results:
            if score < 1.0:
                sys.stderr.write(f"  FAIL {name} (score={score:.2f})\n")
                # Truncate actual output to keep CI logs readable
                actual_str = json.dumps(actual, default=str)[:500]
                sys.stderr.write(f"       actual: {actual_str}\n")
        return 1

    return 0


# ── CI helper: mocked LLM provider ──────────────────────────────────────────

class MockProvider:
    """Returns canned responses from a YAML fixture. Use in CI runs.

    Fixture shape:
        prompts:
          - match: "substring or regex in the prompt"
            response: "canned text"
          - match: "..."
            response: "..."
        default: "fallback response when no match"
    """

    def __init__(self, fixtures_path: str) -> None:
        import re
        fixtures = yaml.safe_load(Path(fixtures_path).read_text()) or {}
        self._rules: list[tuple[Any, str]] = []
        for rule in fixtures.get("prompts", []):
            pattern = rule.get("match", "")
            response = str(rule.get("response", ""))
            # Try compiling as regex; fall back to substring match.
            try:
                compiled = re.compile(pattern)
                self._rules.append((compiled, response))
            except re.error:
                self._rules.append((pattern, response))
        self._default = str(fixtures.get("default", "(no response)"))
        self.enabled = True
        self.name = "mock"

    def generate(self, prompt: str, system: Optional[str] = None,
                 temperature: float = 0.2, max_tokens: Optional[int] = None) -> str:
        import re
        for pattern, response in self._rules:
            if isinstance(pattern, re.Pattern):
                if pattern.search(prompt):
                    return response
            elif isinstance(pattern, str):
                if pattern in prompt:
                    return response
        return self._default
