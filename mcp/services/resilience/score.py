"""Score: per workload, then the overall mean.

Each serving workload (Deployment / StatefulSet / DaemonSet) starts at 100 and
loses the severity weight of every *scored* finding that involves it (advisory
and suppressed findings never count), floored at 0. The overall score is the
unweighted mean, so a small workload can't hide behind a big one. Jobs and
CronJobs still get hygiene findings, but aren't scored — this is about what
serves traffic.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from .model import Inventory
from .types import Finding, WorkloadScore

SCORED_KINDS = ("Deployment", "StatefulSet", "DaemonSet")


def grade_for(score: int, grades: list) -> str:
    for threshold, letter in grades:
        if score >= threshold:
            return str(letter)
    return str(grades[-1][1])


def score_workloads(inv: Inventory, findings: list[Finding], cat: dict[str, Any]) -> tuple[Optional[int], str, list[WorkloadScore]]:
    weights = cat["weights"]
    grades = cat["grades"]
    out: list[WorkloadScore] = []
    for w in inv.workloads:
        if w.kind not in SCORED_KINDS:
            continue
        hits = [f for f in findings if f.scored and any(r.key == w.obj.ref.key for r in f.objects)]
        s = max(0, 100 - sum(int(weights[f.severity]) for f in hits))
        out.append(WorkloadScore(ref=w.obj.ref, score=s, grade=grade_for(s, grades), rules=sorted({f.rule for f in hits})))
    if not out:
        return None, "-", out
    overall = math.floor(sum(ws.score for ws in out) / len(out) + 0.5)
    return overall, grade_for(overall, grades), out
