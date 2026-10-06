"""Pilots eval suite: metrics + golden-dataset invariants (PILOTS_PLAN §Phase 6).

Deterministic, offline. The invariants: every golden upgrade case scores perfectly
(recall/precision/routing/τ all 1.0) and every reconcile case's root cause matches
— so a drift in the planner/analyzer fails here.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from evals.pilots import metrics  # noqa: E402
from evals.pilots.run import main as run_main  # noqa: E402
from evals.pilots.scoring import score_reconcile, score_upgrade  # noqa: E402
from evals.pilots.types import load_reconcile_cases, load_upgrade_cases  # noqa: E402


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
