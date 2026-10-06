"""Pure scoring metrics for the Pilots eval suite.

No dependencies on the Pilot cores — just set/sequence math over labels, so these
are trivially unit-testable.
"""

from __future__ import annotations

from typing import Iterable, Sequence


def recall_precision(found: Iterable[str], expected: Iterable[str]) -> tuple[float, float]:
    """Return (recall, precision) of ``found`` against ``expected`` label sets.

    Empty ``expected`` ⇒ recall 1.0 (nothing to miss); precision is 1.0 only if
    ``found`` is also empty (a false positive on a no-blocker case is precision 0).
    """
    found_s, expected_s = set(found), set(expected)
    tp = len(found_s & expected_s)
    recall = tp / len(expected_s) if expected_s else 1.0
    if found_s:
        precision = tp / len(found_s)
    else:
        precision = 1.0 if not expected_s else 0.0
    return recall, precision


def kendall_tau(produced: Sequence[str], expected: Sequence[str]) -> float:
    """Kendall-τ rank correlation between two orderings of the same items.

    Only items present in ``expected`` are scored (position taken from
    ``expected``); fewer than two comparable items ⇒ 1.0 (nothing to mis-order).
    Identical order ⇒ 1.0, full reversal ⇒ -1.0.
    """
    rank = {x: i for i, x in enumerate(expected)}
    seq = [rank[x] for x in produced if x in rank]
    n = len(seq)
    if n < 2:
        return 1.0
    concordant = discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            if seq[i] < seq[j]:
                concordant += 1
            elif seq[i] > seq[j]:
                discordant += 1
    total = n * (n - 1) // 2
    return (concordant - discordant) / total if total else 1.0


def accuracy(flags: Sequence[bool]) -> float:
    """Fraction of True in ``flags`` (empty ⇒ 1.0)."""
    return sum(1 for f in flags if f) / len(flags) if flags else 1.0
