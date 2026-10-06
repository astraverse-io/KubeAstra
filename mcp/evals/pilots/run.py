"""Pilots eval runner — offline, deterministic (PILOTS_PLAN §Phase 6).

Scores the keyless Pilot cores against the labeled golden set and prints the
headline numbers (blocking recall/precision, routing accuracy, ordering τ,
reconciliation root-cause hit rate). Because the cores are deterministic, the
golden set must score perfectly; any shortfall is a regression and exits non-zero
so CI goes red. Run from ``mcp``: ``python -m evals.pilots.run``.

The cluster-in-the-loop safe-apply eval (success rate, guard false-action rate,
halt/resume, plan-approval coverage) is deferred with Phase 5 part 3 — it needs a
live ``kind`` cluster and cannot run in the offline PR job.
"""

from __future__ import annotations

import sys

from .scoring import score_reconcile, score_upgrade
from .types import load_reconcile_cases, load_upgrade_cases


def main() -> int:
    upgrade_cases = load_upgrade_cases()
    reconcile_cases = load_reconcile_cases()
    print(f"pilots eval: {len(upgrade_cases)} upgrade + {len(reconcile_cases)} reconcile cases")

    failures: list[str] = []

    # ── Upgrade ──────────────────────────────────────────────────────────────
    up_scores = [score_upgrade(c) for c in upgrade_cases]
    for s in up_scores:
        mark = "ok" if s.perfect else "FAIL"
        print(
            f"  [upgrade] {s.case_id}: recall={s.recall:.2f} precision={s.precision:.2f} "
            f"routing={s.routing_accuracy:.2f} order_tau={s.order_tau:.2f}  {mark}"
        )
        if not s.perfect:
            failures.append(f"upgrade/{s.case_id}")
    if up_scores:
        n = len(up_scores)
        print(
            "  [upgrade] MEAN "
            f"recall={sum(s.recall for s in up_scores)/n:.3f} "
            f"precision={sum(s.precision for s in up_scores)/n:.3f} "
            f"routing={sum(s.routing_accuracy for s in up_scores)/n:.3f} "
            f"order_tau={sum(s.order_tau for s in up_scores)/n:.3f}"
        )

    # ── Reconcile ────────────────────────────────────────────────────────────
    rec_scores = [score_reconcile(c) for c in reconcile_cases]
    hits = sum(1 for s in rec_scores if s.hit)
    for s in rec_scores:
        if not s.hit:
            print(f"  [reconcile] {s.case_id}: expected={s.expected} got={s.produced}  FAIL")
            failures.append(f"reconcile/{s.case_id}")
    hit_rate = hits / len(rec_scores) if rec_scores else 1.0
    print(f"  [reconcile] root-cause hit rate = {hit_rate:.3f} ({hits}/{len(rec_scores)})")

    if failures:
        print(f"PILOTS EVAL FAILED — {len(failures)} case(s): {', '.join(failures)}")
        return 1
    print("PILOTS EVAL OK — all golden cases perfect")
    return 0


if __name__ == "__main__":
    sys.exit(main())
