"""Manifest-fix eval runner: offline gate + LIVE scoring (design §7).

The offline path is pure. The LIVE path (fix production + LLM judge) is exercised
with a fake provider that routes on the prompt, so no network is needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.manifest_fix import run as runner  # noqa: E402
from evals.manifest_fix.types import Case, Check, load_cases  # noqa: E402


_GOOD = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n"
    "spec:\n  replicas: 3\n  template:\n    spec:\n      containers:\n"
    "        - name: web\n          image: nginx:1.25.3\n"
)
_CASE = Case(
    id="synthetic",
    symptom="replicas is 0; should be 3",
    kind="Deployment",
    broken=_GOOD.replace("replicas: 3", "replicas: 0"),
    fixed=_GOOD,
    checks=(Check(path="spec.replicas", equals=3),),
)


class _FakeProvider:
    name = "fake"
    enabled = True

    def __init__(self, fix_text: str, judge_text: str):
        self._fix = fix_text
        self._judge = judge_text

    def generate(self, prompt, system=None, max_tokens=None):
        if "corrected manifest" in prompt:
            return self._fix
        if "single number" in prompt:
            return self._judge
        return ""


# ── offline ──────────────────────────────────────────────────────────────────


def test_run_offline_passes_on_real_dataset():
    report = runner.run_offline(load_cases())
    assert report.ok, report.failures
    assert report.total >= 8


def test_run_offline_flags_a_bad_golden():
    # Golden fix that does NOT satisfy its own check → offline must catch it.
    bad = Case(
        id="bad", symptom="s", kind="Deployment",
        broken=_GOOD, fixed=_GOOD,  # fixed still has replicas: 3...
        checks=(Check(path="spec.replicas", equals=5),),  # ...but check wants 5
    )
    report = runner.run_offline([bad])
    assert not report.ok
    assert any("golden fix scored" in f for f in report.failures)


def test_run_offline_flags_noop_case():
    # broken == fixed and the check already passes → the case tests nothing.
    noop = Case(
        id="noop", symptom="s", kind="Deployment",
        broken=_GOOD, fixed=_GOOD,
        checks=(Check(path="spec.replicas", equals=3),),
    )
    report = runner.run_offline([noop])
    assert not report.ok
    assert any("no-op" in f for f in report.failures)


# ── helpers ──────────────────────────────────────────────────────────────────


def test_strip_fences():
    assert runner._strip_fences("```yaml\nkind: X\n```") == "kind: X"
    assert runner._strip_fences("kind: X") == "kind: X"


def test_judge_parses_various_formats():
    p = _FakeProvider(_GOOD, "0.9")
    assert runner.judge_fix(p, _CASE, _GOOD) == 0.9
    assert runner.judge_fix(_FakeProvider(_GOOD, "score is 1.0"), _CASE, _GOOD) == 1.0
    assert runner.judge_fix(_FakeProvider(_GOOD, "nonsense"), _CASE, _GOOD) is None


def test_produce_fix_strips_fences():
    p = _FakeProvider("```yaml\n" + _GOOD + "```", "1.0")
    assert runner.produce_fix(p, _CASE).startswith("apiVersion: apps/v1")


# ── LIVE aggregation ─────────────────────────────────────────────────────────


def test_run_live_scores_a_correct_fix():
    provider = _FakeProvider(_GOOD, "0.95")
    report = runner.run_live([_CASE], provider, emit=lambda *_: None)
    assert report.mean_objective == 1.0
    assert report.mean_judge == 0.95
    assert report.ok


def test_run_live_scores_a_wrong_fix_low():
    # Model returns the still-broken manifest → objective below threshold.
    provider = _FakeProvider(_CASE.broken, "0.1")
    report = runner.run_live([_CASE], provider, emit=lambda *_: None)
    assert report.mean_objective < runner.LIVE_PASS_THRESHOLD
    assert not report.ok


def test_run_live_survives_provider_error():
    class _Boom:
        def generate(self, *a, **k):
            raise RuntimeError("provider down")

    report = runner.run_live([_CASE], _Boom(), judge=False, emit=lambda *_: None)
    assert report.results[0].objective == 0.0
    assert not report.ok
