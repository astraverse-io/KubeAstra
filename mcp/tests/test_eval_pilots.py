"""Pilots eval suite: metrics + golden-dataset invariants (PILOTS_PLAN §Phase 6).

Deterministic, offline. The invariants: every golden upgrade case scores perfectly
(recall/precision/routing/τ all 1.0), every reconcile case's root cause matches,
and every resilience case fires exactly its labeled rules — so a drift in the
planner/analyzer/rules fails here.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from evals.pilots import metrics  # noqa: E402
from evals.pilots.run import main as run_main  # noqa: E402
from evals.pilots.scoring import per_rule, score_reconcile, score_resilience, score_upgrade  # noqa: E402
from evals.pilots.types import (  # noqa: E402
    ResilienceCase,
    load_reconcile_cases,
    load_resilience_cases,
    load_upgrade_cases,
)


# ── metrics units ────────────────────────────────────────────────────────────


def test_recall_precision_basic():
    r, p = metrics.recall_precision({"a", "b"}, {"a", "b", "c"})
    assert r == pytest.approx(2 / 3) and p == 1.0
    r, p = metrics.recall_precision({"a", "x"}, {"a"})
    assert r == 1.0 and p == pytest.approx(0.5)


def test_recall_precision_empty_expected():
    assert metrics.recall_precision([], []) == (1.0, 1.0)
    # false positive on a no-blocker case → precision 0
    assert metrics.recall_precision(["x"], []) == (1.0, 0.0)


def test_kendall_tau_identical_and_reversed():
    assert metrics.kendall_tau(["a", "b", "c"], ["a", "b", "c"]) == 1.0
    assert metrics.kendall_tau(["c", "b", "a"], ["a", "b", "c"]) == -1.0
    assert metrics.kendall_tau(["a"], ["a", "b"]) == 1.0  # <2 comparable


def test_accuracy():
    assert metrics.accuracy([True, True, False]) == pytest.approx(2 / 3)
    assert metrics.accuracy([]) == 1.0


# ── golden-dataset invariants ────────────────────────────────────────────────


def test_datasets_load():
    up = load_upgrade_cases()
    rec = load_reconcile_cases()
    assert len(up) >= 4 and len(rec) >= 5
    assert len({c.id for c in up}) == len(up)
    assert len({c.id for c in rec}) == len(rec)


@pytest.mark.parametrize("case", load_upgrade_cases(), ids=lambda c: c.id)
def test_golden_upgrade_scores_perfect(case):
    s = score_upgrade(case)
    assert s.perfect, (
        f"{case.id}: recall={s.recall} precision={s.precision} "
        f"routing={s.routing_accuracy} tau={s.order_tau}"
    )


@pytest.mark.parametrize("case", load_reconcile_cases(), ids=lambda c: c.id)
def test_golden_reconcile_hits(case):
    s = score_reconcile(case)
    assert s.hit, f"{case.id}: expected {s.expected}, got {s.produced}"


def test_runner_exits_zero_on_golden():
    assert run_main() == 0


# ── resilience ───────────────────────────────────────────────────────────────


def test_resilience_dataset_loads_with_clean_and_dirty_cases():
    cases = load_resilience_cases()
    assert len(cases) >= 10 and len({c.id for c in cases}) == len(cases)
    assert any(not c.expect_rules for c in cases), "need clean cases, or false positives go unmeasured"


@pytest.mark.parametrize("case", load_resilience_cases(), ids=lambda c: c.id)
def test_golden_resilience_fires_exactly_its_rules(case):
    s = score_resilience(case)
    assert s.perfect, (
        f"{case.id}: missed={sorted(s.expected - s.found)} extra={sorted(s.found - s.expected)} "
        f"score={s.score} score_ok={s.score_ok} fixes={s.fix_mismatches}"
    )


def test_a_wrong_label_fails_the_gate():
    good = next(c for c in load_resilience_cases() if c.id == "single-replica-webhook")
    for wrong in (
        ResilienceCase(**{**good.__dict__, "expect_rules": frozenset({"RES002"})}),           # RES008 becomes "extra"
        ResilienceCase(**{**good.__dict__, "expect_rules": good.expect_rules | {"RES005"}}),  # RES005 "missed"
        ResilienceCase(**{**good.__dict__, "expect_score": 50}),
        ResilienceCase(**{**good.__dict__, "expect_fixes": {"RES002": "field"}}),
    ):
        assert not score_resilience(wrong).perfect


def test_per_rule_precision_and_recall():
    cases = [c for c in load_resilience_cases() if c.id in ("single-replica-webhook", "metrics-only-controller")]
    scores = [score_resilience(c) for c in cases]
    assert per_rule(scores) == {"RES002": (1.0, 1.0, 1), "RES008": (1.0, 1.0, 1)}
    # a rule firing on a case that doesn't expect it costs precision
    clean = next(c for c in load_resilience_cases() if c.id == "metrics-only-controller")
    fp = score_resilience(ResilienceCase(**{**clean.__dict__, "expect_rules": frozenset()}))
    fp.found = frozenset({"RES002"})
    assert per_rule([fp])["RES002"] == (0.0, 1.0, 0)
