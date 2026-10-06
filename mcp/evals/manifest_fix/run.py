"""Manifest-fix eval runner (design §7).

Two modes, selected by the ``EVAL_LIVE_LLM`` env var (matching evals.yml):

- **offline** (default) — deterministic, free, runs on every PR. Scores each
  golden fix (must be 1.0) and each broken manifest (must be < 1.0). This is a
  coherence gate on the suite itself: a case file that drifts fails CI here.
- **LIVE** (``EVAL_LIVE_LLM=true``) — asks a real model to *produce* a fix for
  each case from the symptom + broken manifest, scores it with the objective
  validators, and adds an LLM-as-judge quality score. Skips cleanly (exit 0)
  when no provider is configured, so the nightly job is a no-op without a key.

Invoked as ``python -m evals.manifest_fix.run`` from the ``mcp`` working dir.
Exit code is non-zero on failure/regression so CI goes red.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .types import Case, load_cases
from .validators import score_fix

# A LIVE run passes when the mean objective score clears this bar. Conservative
# to start; tighten as the suite and harness mature.
LIVE_PASS_THRESHOLD = 0.80

_FIX_PROMPT = """You are fixing a broken Kubernetes manifest.

Symptom:
{symptom}

Broken manifest:
```yaml
{broken}
```

Return ONLY the corrected manifest as YAML. No explanation, no code fence."""

_JUDGE_PROMPT = """Score how well a candidate fix resolves a Kubernetes manifest problem.

Symptom:
{symptom}

Known-good reference fix:
```yaml
{reference}
```

Candidate fix:
```yaml
{candidate}
```

Reply with a single number from 0.0 to 1.0: 1.0 = correct and minimal (resolves
the symptom, no unrelated changes), 0.0 = wrong or broken. Reply with the number only."""

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9]*\s*|\s*```$")
_FLOAT_RE = re.compile(r"[-+]?\d*\.?\d+")


def _strip_fences(text: str) -> str:
    """Strip a leading/trailing Markdown code fence if the model added one."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = _FENCE_RE.sub("", t)
        if t.endswith("```"):
            t = t[: t.rfind("```")]
    return t.strip()


# ── Offline ──────────────────────────────────────────────────────────────────


@dataclass
class OfflineReport:
    total: int
    failures: list[str]

    @property
    def ok(self) -> bool:
        return not self.failures


def run_offline(cases: list[Case]) -> OfflineReport:
    """Verify suite coherence: golden fixes score 1.0, broken score < 1.0."""
    failures: list[str] = []
    for case in cases:
        good = score_fix(case.fixed, case)
        if good.score != 1.0:
            failures.append(f"{case.id}: golden fix scored {good.score:.2f} ({good.detail})")
        bad = score_fix(case.broken, case)
        if bad.score >= 1.0:
            failures.append(f"{case.id}: broken manifest scored {bad.score:.2f} (case is a no-op)")
    return OfflineReport(total=len(cases), failures=failures)


# ── LIVE ─────────────────────────────────────────────────────────────────────


def produce_fix(provider: Any, case: Case) -> str:
    """Ask the model to produce a corrected manifest (LIVE)."""
    prompt = _FIX_PROMPT.format(symptom=case.symptom, broken=case.broken)
    text = provider.generate(prompt, system="You are a Kubernetes expert.", max_tokens=1024)
    return _strip_fences(text)


def judge_fix(provider: Any, case: Case, candidate: str) -> Optional[float]:
    """LLM-as-judge quality score in [0, 1], or None if unparseable (LIVE)."""
    prompt = _JUDGE_PROMPT.format(symptom=case.symptom, reference=case.fixed, candidate=candidate)
    text = provider.generate(prompt, system="You are a strict grader.", max_tokens=16)
    m = _FLOAT_RE.search(text or "")
    if not m:
        return None
    try:
        return max(0.0, min(1.0, float(m.group(0))))
    except ValueError:
        return None


@dataclass
class LiveCaseResult:
    case_id: str
    objective: float
    judge: Optional[float]


@dataclass
class LiveReport:
    results: list[LiveCaseResult]

    @property
    def mean_objective(self) -> float:
        return sum(r.objective for r in self.results) / len(self.results) if self.results else 0.0

    @property
    def mean_judge(self) -> Optional[float]:
        judged = [r.judge for r in self.results if r.judge is not None]
        return sum(judged) / len(judged) if judged else None

    @property
    def ok(self) -> bool:
        return bool(self.results) and self.mean_objective >= LIVE_PASS_THRESHOLD


def run_live(
    cases: list[Case],
    provider: Any,
    *,
    judge: bool = True,
    emit: Callable[[str], None] = print,
) -> LiveReport:
    """Produce + score a fix for every case with a live provider."""
    results: list[LiveCaseResult] = []
    for case in cases:
        try:
            candidate = produce_fix(provider, case)
        except Exception as exc:  # a provider error shouldn't abort the whole suite
            emit(f"  ! {case.id}: fix generation failed: {exc}")
            results.append(LiveCaseResult(case_id=case.id, objective=0.0, judge=None))
            continue
        objective = score_fix(candidate, case).score
        quality = judge_fix(provider, case, candidate) if judge else None
        q = f" judge={quality:.2f}" if quality is not None else ""
        emit(f"  {case.id}: objective={objective:.2f}{q}")
        results.append(LiveCaseResult(case_id=case.id, objective=objective, judge=quality))
    return LiveReport(results=results)


# ── Entry point ──────────────────────────────────────────────────────────────


def _live_enabled() -> bool:
    return os.getenv("EVAL_LIVE_LLM", "false").strip().lower() in ("1", "true", "yes")


def main() -> int:
    cases = load_cases()
    print(f"manifest-fix eval: {len(cases)} cases")

    if not _live_enabled():
        report = run_offline(cases)
        if report.ok:
            print(f"OFFLINE OK — {report.total} cases coherent (golden=1.0, broken<1.0)")
            return 0
        print("OFFLINE FAILED:")
        for f in report.failures:
            print(f"  - {f}")
        return 1

    # LIVE: needs a configured provider; skip cleanly if none.
    try:
        from services.llm import get_provider
        provider = get_provider()
    except Exception as exc:  # noqa: BLE001 - any import/config failure => skip
        print(f"LIVE skipped — provider unavailable: {exc}")
        return 0
    if not getattr(provider, "enabled", False):
        print("LIVE skipped — no provider configured (set an API key to run).")
        return 0

    print(f"LIVE — producing + scoring fixes with {getattr(provider, 'name', '?')}")
    report = run_live(cases, provider)
    mj = report.mean_judge
    print(
        f"LIVE mean objective={report.mean_objective:.2f} "
        f"(threshold {LIVE_PASS_THRESHOLD:.2f})"
        + (f", mean judge={mj:.2f}" if mj is not None else "")
    )
    if report.ok:
        print("LIVE OK")
        return 0
    print("LIVE FAILED — mean objective below threshold")
    return 1


if __name__ == "__main__":
    sys.exit(main())
