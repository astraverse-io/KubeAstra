#!/usr/bin/env python3
"""DeepEval run comparison script.

Loads two JSON report files (baseline vs new run) and checks if quality or
safety has regressed beyond acceptable limits. Generates a Markdown summary.
"""

import argparse
import json
import sys
from pathlib import Path


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        print(f"Error reading JSON file {path}: {exc}", file=sys.stderr)
        sys.exit(2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare DeepEval agent runs.")
    parser.add_argument("baseline", type=Path, help="Path to baseline JSON report.")
    parser.add_argument("new_run", type=Path, help="Path to new run JSON report.")
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=None,
        help="Path to write a Markdown comparison summary.",
    )
    args = parser.parse_args()

    baseline = load_json(args.baseline)
    new_run = load_json(args.new_run)

    b_scenarios = {s["scenario"]["id"]: s for s in baseline.get("scenarios", [])}
    n_scenarios = {s["scenario"]["id"]: s for s in new_run.get("scenarios", [])}

    common_ids = sorted(list(b_scenarios.keys() & n_scenarios.keys()))
    if not common_ids:
        print("Error: No common scenarios found between the two reports.", file=sys.stderr)
        return 2

    # Track metrics
    base_safety_violations = []
    safety_violations = []
    
    base_tool_choice_passed = 0
    new_tool_choice_passed = 0
    
    base_relevancy_scores = []
    new_relevancy_scores = []
    
    base_quality_scores = []
    new_quality_scores = []
    
    scenario_details = []

    for sid in common_ids:
        b_sc = b_scenarios[sid]
        n_sc = n_scenarios[sid]

        b_det = b_sc.get("deterministic", {})
        n_det = n_sc.get("deterministic", {})

        # Safety Check (both runs)
        b_safety_passed = b_det.get("safety_passed", True)
        if not b_safety_passed and not b_det.get("skipped"):
            base_safety_violations.append(sid)

        safety_passed = n_det.get("safety_passed", True)
        if not safety_passed and not n_det.get("skipped"):
            safety_violations.append(sid)

        # Tool Choice
        if b_det.get("tool_choice_passed") is True and not b_det.get("skipped"):
            base_tool_choice_passed += 1
        if n_det.get("tool_choice_passed") is True and not n_det.get("skipped"):
            new_tool_choice_passed += 1

        # Extract Metrics
        b_metrics = {m["name"]: m for m in b_sc.get("metrics", [])}
        n_metrics = {m["name"]: m for m in n_sc.get("metrics", [])}

        # Relevancy (Only pair-wise inclusion)
        b_rel = b_metrics.get("answer_relevancy", {})
        n_rel = n_metrics.get("answer_relevancy", {})
        
        rel_base_val = b_rel.get("mean") if (b_rel and not b_rel.get("skipped")) else None
        rel_new_val = n_rel.get("mean") if (n_rel and not n_rel.get("skipped")) else None
        
        if rel_base_val is not None and rel_new_val is not None:
            base_relevancy_scores.append(rel_base_val)
            new_relevancy_scores.append(rel_new_val)

        # Quality (Only pair-wise inclusion)
        b_q = b_metrics.get("k8s_incident_quality", {})
        n_q = n_metrics.get("k8s_incident_quality", {})

        q_base_val = b_q.get("mean") if (b_q and not b_q.get("skipped")) else None
        q_new_val = n_q.get("mean") if (n_q and not n_q.get("skipped")) else None

        if q_base_val is not None and q_new_val is not None:
            base_quality_scores.append(q_base_val)
            new_quality_scores.append(q_new_val)

        scenario_details.append({
            "id": sid,
            "safety_passed": safety_passed if not n_det.get("skipped") else None,
            "tool_choice_base": b_det.get("tool_choice_passed") if not b_det.get("skipped") else None,
            "tool_choice_new": n_det.get("tool_choice_passed") if not n_det.get("skipped") else None,
            "rel_base": rel_base_val,
            "rel_new": rel_new_val,
            "quality_base": q_base_val,
            "quality_new": q_new_val,
        })

    # Calculations
    tool_choice_drop = base_tool_choice_passed - new_tool_choice_passed
    
    base_rel_avg = sum(base_relevancy_scores) / len(base_relevancy_scores) if base_relevancy_scores else 0.0
    new_rel_avg = sum(new_relevancy_scores) / len(new_relevancy_scores) if new_relevancy_scores else 0.0
    rel_diff = new_rel_avg - base_rel_avg

    base_q_avg = sum(base_quality_scores) / len(base_quality_scores) if base_quality_scores else 0.0
    new_q_avg = sum(new_quality_scores) / len(new_quality_scores) if new_quality_scores else 0.0
    q_diff = new_q_avg - base_q_avg

    # Gate evaluations
    safety_failed = len(safety_violations) > 0
    tool_choice_failed = tool_choice_drop >= 2
    relevancy_failed = rel_diff <= -0.05
    quality_failed = q_diff <= -0.05

    regression = safety_failed or tool_choice_failed or relevancy_failed or quality_failed

    # Formatting results
    safety_status = "❌ FAIL" if safety_failed else "✅ PASS"
    tool_choice_status = "❌ FAIL" if tool_choice_failed else "✅ PASS"
    relevancy_status = "❌ FAIL" if relevancy_failed else "✅ PASS"
    quality_status = "❌ FAIL" if quality_failed else "✅ PASS"

    # Markdown generation
    b_safety_text = "All Passed" if not base_safety_violations else f"{len(base_safety_violations)} violations"
    md_lines = [
        "<!-- deepeval-comparison-report -->",
        "# DeepEval Agent Quality Comparison Report",
        "",
        "## Summary",
        "",
        "| Metric | Baseline | New Run | Delta | Status |",
        "| :--- | :--- | :--- | :--- | :--- |",
        f"| **Safety Checked** | {b_safety_text} | {len(safety_violations)} violations | - | {safety_status} |",
        f"| **Tool Choice Pass Rate** | {base_tool_choice_passed}/{len(common_ids)} | {new_tool_choice_passed}/{len(common_ids)} | {-tool_choice_drop:+} | {tool_choice_status} |",
        f"| **Answer Relevancy Mean** | {base_rel_avg:.4f} | {new_rel_avg:.4f} | {rel_diff:+.4f} | {relevancy_status} |",
        f"| **Incident Quality Mean** | {base_q_avg:.4f} | {new_q_avg:.4f} | {q_diff:+.4f} | {quality_status} |",
        "",
    ]

    if safety_failed:
        md_lines.append("> [!CAUTION]")
        md_lines.append("> **Safety Regression Detected!** The following scenarios called a forbidden (`must_not_call`) tool:")
        for violation in safety_violations:
            md_lines.append(f"> - `{violation}`")
        md_lines.append("")

    if base_safety_violations:
        md_lines.append("<details>")
        md_lines.append("<summary>⚠️ <b>Baseline Safety Violations Details</b></summary>")
        md_lines.append("")
        md_lines.append("The baseline run contained safety violations in these scenarios:")
        for violation in base_safety_violations:
            md_lines.append(f"- `{violation}`")
        md_lines.append("</details>")
        md_lines.append("")

    if tool_choice_failed:
        md_lines.append(f"> [!WARNING]")
        md_lines.append(f"> **Tool Choice Regression!** Drop of {tool_choice_drop} scenarios exceeds tolerance of 2.")
        md_lines.append("")

    if relevancy_failed:
        md_lines.append(f"> [!WARNING]")
        md_lines.append(f"> **Answer Relevancy Regression!** Drop of {abs(rel_diff):.4f} exceeds tolerance of 0.05.")
        md_lines.append("")

    if quality_failed:
        md_lines.append(f"> [!WARNING]")
        md_lines.append(f"> **Incident Quality Regression!** Drop of {abs(q_diff):.4f} exceeds tolerance of 0.05.")
        md_lines.append("")

    md_lines.extend([
        "## Per-Scenario Breakdown",
        "",
        "| Scenario | Safety | Tool Choice (Base → New) | Answer Relevancy (Base → New) | Incident Quality (Base → New) |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ])

    for row in scenario_details:
        s_pass = "✅" if row["safety_passed"] is True else ("❌" if row["safety_passed"] is False else "➖")
        
        tc_b = "✅" if row["tool_choice_base"] is True else ("❌" if row["tool_choice_base"] is False else "➖")
        tc_n = "✅" if row["tool_choice_new"] is True else ("❌" if row["tool_choice_new"] is False else "➖")
        tc_cell = f"{tc_b} → {tc_n}"

        rel_b = f"{row['rel_base']:.4f}" if row["rel_base"] is not None else "➖"
        rel_n = f"{row['rel_new']:.4f}" if row["rel_new"] is not None else "➖"
        rel_cell = f"{rel_b} → {rel_n}"

        q_b = f"{row['quality_base']:.4f}" if row["quality_base"] is not None else "➖"
        q_n = f"{row['quality_new']:.4f}" if row["quality_new"] is not None else "➖"
        q_cell = f"{q_b} → {q_n}"

        md_lines.append(
            f"| `{row['id']}` | {s_pass} | {tc_cell} | {rel_cell} | {q_cell} |"
        )

    markdown_report = "\n".join(md_lines) + "\n"

    # Write report
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(markdown_report)

    # Print summary
    print(markdown_report)

    if regression:
        print("CI Gate Failed: Regression detected beyond configured tolerance.", file=sys.stderr)
        return 1

    print("CI Gate Passed: No regressions detected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
