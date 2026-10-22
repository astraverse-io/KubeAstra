"""Summarize a sweep.sh run: per-chart score, and every finding by rule.

    python3 scripts/resilience-corpus/summarize.py OUT_DIR [--high]

--high prints every high-severity finding in full, for the by-hand review a
sweep exists for: a high finding on a popular chart's defaults is either a real
problem in that chart or a false positive in a rule.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    out = Path(argv[0])
    show_high = "--high" in argv
    versions = {}
    for line in (out / "versions.tsv").read_text().splitlines():
        release, chart, version, _repo = line.split("\t")
        versions[release] = f"{chart} {version}"

    by_rule: Counter = Counter()
    charts_by_rule: dict[str, set] = defaultdict(set)
    rows = []
    highs = []
    for release in sorted(versions):
        report_path = out / release / "report.json"
        if not report_path.is_file() or not report_path.read_text().strip():
            rows.append((release, "not scored", "", ""))
            continue
        r = json.loads(report_path.read_text())
        scored = [f for f in r["findings"] if not f["advisory"] and not f["suppressed"]]
        for f in scored:
            by_rule[f["rule"]] += 1
            charts_by_rule[f["rule"]].add(release)
            if f["severity"] == "high":
                highs.append((release, f))
        rows.append((release, f"{r['score']} {r['grade']}", len(r["workloads"]), len(scored)))

    print(f"{'chart':<44} {'score':>7} {'workloads':>9} {'findings':>8}")
    for release, score, workloads, findings in rows:
        print(f"{versions[release]:<44} {score:>7} {workloads!s:>9} {findings!s:>8}")
    print("\nrule    findings  charts")
    for rule in sorted(by_rule):
        print(f"{rule}  {by_rule[rule]:>8}  {len(charts_by_rule[rule]):>6}  {', '.join(sorted(charts_by_rule[rule]))}")
    if show_high:
        print(f"\nhigh-severity findings ({len(highs)}):")
        for release, f in highs:
            objs = " + ".join(f"{o['kind']}/{o['name']}" for o in f["objects"])
            print(f"- [{release}] {f['rule']} {objs}: {f['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
