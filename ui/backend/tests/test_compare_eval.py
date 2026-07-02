"""Unit and integration tests for eval comparison script gating logic (Phase 3)."""

from pathlib import Path
import sys
import json
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
for path in (BACKEND_DIR,):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# Add the scripts directory to path to import compare_eval_runs
SCRIPTS_DIR = BACKEND_DIR.parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import compare_eval_runs


def _create_mock_report(
    scenario_id="s1",
    safety_passed=True,
    tool_choice_passed=True,
    relevancy=0.85,
    quality=0.75,
    skipped=False,
):
    return {
        "run": {
            "run_id": "test-run",
            "started_at": 1000,
            "ended_at": 1010,
            "backend_url": "http://localhost:8000",
            "judge_model": "gemini-2.5-flash",
            "judge_repetitions": 1,
            "max_judge_requests": 250,
            "judge_requests_used": 10,
            "no_network": False,
            "thresholds": {
                "answer_relevancy": 0.8,
                "faithfulness": 0.8,
                "k8s_incident_quality": 0.7,
            },
            "scenario_count": 1,
        },
        "scenarios": [
            {
                "scenario": {
                    "id": scenario_id,
                    "prompt": "test prompt",
                    "expected_tools": ["t1"],
                    "must_not_call": ["t2"],
                },
                "chat": {
                    "scenario_id": scenario_id,
                    "prompt": "test prompt",
                    "tool_used": "t1",
                    "actual_output": "test answer",
                    "rag_mode": "cold",
                    "retrieval_context": [],
                },
                "metrics": [
                    {
                        "name": "answer_relevancy",
                        "mean": relevancy,
                        "skipped": skipped,
                        "threshold": 0.8,
                    },
                    {
                        "name": "k8s_incident_quality",
                        "mean": quality,
                        "skipped": skipped,
                        "threshold": 0.7,
                    },
                ],
                "deterministic": {
                    "tool_used": "t1",
                    "safety_passed": safety_passed,
                    "tool_choice_passed": tool_choice_passed,
                    "skipped": skipped,
                },
            }
        ],
    }


def test_compare_eval_identical_reports_pass(tmp_path, monkeypatch):
    base_data = _create_mock_report()
    new_data = _create_mock_report()

    base_file = tmp_path / "base.json"
    new_file = tmp_path / "new.json"
    md_file = tmp_path / "report.md"

    base_file.write_text(json.dumps(base_data))
    new_file.write_text(json.dumps(new_data))

    monkeypatch.setattr(
        sys,
        "argv",
        ["compare_eval_runs.py", str(base_file), str(new_file), "--markdown-output", str(md_file)],
    )

    exit_code = compare_eval_runs.main()
    assert exit_code == 0
    assert md_file.exists()
    
    md_content = md_file.read_text()
    assert "✅ PASS" in md_content
    assert "safety" in md_content.lower()


def test_compare_eval_safety_violation_fails(tmp_path, monkeypatch):
    base_data = _create_mock_report()
    # Create safety violation in new run
    new_data = _create_mock_report(safety_passed=False)

    base_file = tmp_path / "base.json"
    new_file = tmp_path / "new.json"

    base_file.write_text(json.dumps(base_data))
    new_file.write_text(json.dumps(new_data))

    monkeypatch.setattr(
        sys, "argv", ["compare_eval_runs.py", str(base_file), str(new_file)]
    )

    exit_code = compare_eval_runs.main()
    assert exit_code == 1


def test_compare_eval_tool_choice_drop_fails(tmp_path, monkeypatch):
    # To test tool choice drop of >= 2, we need at least 2 scenarios
    base_data = _create_mock_report(scenario_id="s1")
    base_data["scenarios"].append(_create_mock_report(scenario_id="s2")["scenarios"][0])

    # New data has both scenarios failed on tool choice
    new_data = _create_mock_report(scenario_id="s1", tool_choice_passed=False)
    new_data["scenarios"].append(
        _create_mock_report(scenario_id="s2", tool_choice_passed=False)["scenarios"][0]
    )

    base_file = tmp_path / "base.json"
    new_file = tmp_path / "new.json"

    base_file.write_text(json.dumps(base_data))
    new_file.write_text(json.dumps(new_data))

    monkeypatch.setattr(
        sys, "argv", ["compare_eval_runs.py", str(base_file), str(new_file)]
    )

    exit_code = compare_eval_runs.main()
    assert exit_code == 1


def test_compare_eval_relevancy_drop_fails(tmp_path, monkeypatch):
    base_data = _create_mock_report(relevancy=0.90)
    # Relevancy drops by 0.06 (exceeding tolerance of 0.05)
    new_data = _create_mock_report(relevancy=0.84)

    base_file = tmp_path / "base.json"
    new_file = tmp_path / "new.json"

    base_file.write_text(json.dumps(base_data))
    new_file.write_text(json.dumps(new_data))

    monkeypatch.setattr(
        sys, "argv", ["compare_eval_runs.py", str(base_file), str(new_file)]
    )

    exit_code = compare_eval_runs.main()
    assert exit_code == 1


def test_compare_eval_quality_drop_fails(tmp_path, monkeypatch):
    base_data = _create_mock_report(quality=0.80)
    # Incident quality drops by 0.05
    new_data = _create_mock_report(quality=0.75)

    base_file = tmp_path / "base.json"
    new_file = tmp_path / "new.json"

    base_file.write_text(json.dumps(base_data))
    new_file.write_text(json.dumps(new_data))

    monkeypatch.setattr(
        sys, "argv", ["compare_eval_runs.py", str(base_file), str(new_file)]
    )

    exit_code = compare_eval_runs.main()
    assert exit_code == 1


def test_compare_eval_relevancy_drop_at_boundary_fails(tmp_path, monkeypatch):
    base_data = _create_mock_report(relevancy=0.90)
    # Relevancy drops by exactly 0.05 (should fail)
    new_data = _create_mock_report(relevancy=0.85)

    base_file = tmp_path / "base.json"
    new_file = tmp_path / "new.json"
    base_file.write_text(json.dumps(base_data))
    new_file.write_text(json.dumps(new_data))

    monkeypatch.setattr(
        sys, "argv", ["compare_eval_runs.py", str(base_file), str(new_file)]
    )

    exit_code = compare_eval_runs.main()
    assert exit_code == 1


def test_compare_eval_relevancy_drop_below_threshold_passes(tmp_path, monkeypatch):
    base_data = _create_mock_report(relevancy=0.90)
    # Relevancy drops by 0.04 (less than 0.05, should pass)
    new_data = _create_mock_report(relevancy=0.86)

    base_file = tmp_path / "base.json"
    new_file = tmp_path / "new.json"
    base_file.write_text(json.dumps(base_data))
    new_file.write_text(json.dumps(new_data))

    monkeypatch.setattr(
        sys, "argv", ["compare_eval_runs.py", str(base_file), str(new_file)]
    )

    exit_code = compare_eval_runs.main()
    assert exit_code == 0


def test_compare_eval_tool_choice_drop_of_one_passes(tmp_path, monkeypatch):
    base_data = _create_mock_report(scenario_id="s1")
    base_data["scenarios"].append(_create_mock_report(scenario_id="s2")["scenarios"][0])

    new_data = _create_mock_report(scenario_id="s1", tool_choice_passed=False)
    new_data["scenarios"].append(_create_mock_report(scenario_id="s2")["scenarios"][0])

    base_file = tmp_path / "base.json"
    new_file = tmp_path / "new.json"
    base_file.write_text(json.dumps(base_data))
    new_file.write_text(json.dumps(new_data))

    monkeypatch.setattr(
        sys, "argv", ["compare_eval_runs.py", str(base_file), str(new_file)]
    )

    exit_code = compare_eval_runs.main()
    assert exit_code == 0


def test_compare_eval_no_common_scenarios_fails(tmp_path, monkeypatch):
    base_data = _create_mock_report(scenario_id="s1")
    new_data = _create_mock_report(scenario_id="s2")

    base_file = tmp_path / "base.json"
    new_file = tmp_path / "new.json"
    base_file.write_text(json.dumps(base_data))
    new_file.write_text(json.dumps(new_data))

    monkeypatch.setattr(
        sys, "argv", ["compare_eval_runs.py", str(base_file), str(new_file)]
    )

    exit_code = compare_eval_runs.main()
    assert exit_code == 2


def test_compare_eval_asymmetric_scenarios_ignored(tmp_path, monkeypatch):
    # base [0.85, 0.95], new [0.85, skipped]
    # Non-pairwise: base=0.90, new=0.85, diff=-0.05  → FAIL
    # Pairwise:     base=0.85, new=0.85, diff= 0.00  → PASS
    base_data = _create_mock_report(scenario_id="s1", relevancy=0.85)
    base_data["scenarios"].append(
        _create_mock_report(scenario_id="s2", relevancy=0.95)["scenarios"][0]
    )

    new_data = _create_mock_report(scenario_id="s1", relevancy=0.85)
    new_data["scenarios"].append(
        _create_mock_report(scenario_id="s2", relevancy=0.50, skipped=True)["scenarios"][0]
    )

    base_file = tmp_path / "base.json"
    new_file = tmp_path / "new.json"
    base_file.write_text(json.dumps(base_data))
    new_file.write_text(json.dumps(new_data))

    monkeypatch.setattr(
        sys, "argv", ["compare_eval_runs.py", str(base_file), str(new_file)]
    )

    exit_code = compare_eval_runs.main()
    # If pairwise works, rel_diff is 0.0 (passes). If not, we'd have a gain or drop.
    assert exit_code == 0

