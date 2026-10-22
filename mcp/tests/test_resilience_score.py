"""Resilience Pilot Phase 0: scoring, the golden scenario, and repo config.

The golden file pins the whole report for a small realistic app
(``fixtures/resilience/payments/``). If a rule, weight or severity change moves
it, that must be a deliberate edit of ``payments.expected.json`` — regenerate
it with ``KUBEASTRA_UPDATE_GOLDEN=1 pytest mcp/tests/test_resilience_score.py``
and review the diff.
"""
import json
import os
import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from services.resilience import assess, load_rules, scan_path  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "resilience"
GOLDEN = FIXTURES / "payments.expected.json"


def _summary(report) -> dict:
    return {
        "score": report.score,
        "grade": report.grade,
        "workloads": [
            {"kind": w.ref.kind, "name": w.ref.name, "score": w.score, "grade": w.grade, "rules": w.rules}
            for w in report.workloads
        ],
        "findings": [
            {
                "rule": f.rule,
                "severity": f.severity,
                "advisory": f.advisory,
                "suppressed": f.suppressed,
                "objects": [f"{Path(r.file).name}:{r.line} {r.kind}/{r.name}" for r in f.objects],
            }
            for f in report.findings
        ],
    }


def test_golden_payments_report():
    got = _summary(assess(scan_path(FIXTURES / "payments")))
    if os.environ.get("KUBEASTRA_UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=2) + "\n")
    assert got == json.loads(GOLDEN.read_text())


def test_report_round_trips_as_json():
    report = assess(scan_path(FIXTURES / "payments"))
    data = json.loads(json.dumps(report.to_dict()))
    assert data["score"] == report.score
    assert {f["rule"] for f in data["findings"]} == {f.rule for f in report.findings}


def test_no_workloads_is_not_a_perfect_score():
    report = assess([{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "x"}}])
    assert report.score is None and report.grade == "-"
    assert any("No workloads" in n for n in report.notes)


def test_score_floors_at_zero():
    cat = load_rules()
    cat["weights"] = {"high": 100, "medium": 100, "low": 100}
    report = assess(scan_path(FIXTURES / "payments"), catalogue=cat)
    assert min(w.score for w in report.workloads) == 0


def test_config_disable_and_severity_change_the_score():
    base = assess(scan_path(FIXTURES / "payments"))
    disabled = assess(scan_path(FIXTURES / "payments"), config={"disable": ["RES001"]})
    assert "RES001" not in {f.rule for f in disabled.findings}
    assert disabled.score > base.score
    raised = assess(scan_path(FIXTURES / "payments"), config={"severity": {"RES010": "high"}})
    assert next(f for f in raised.findings if f.rule == "RES010").severity == "high"
    assert raised.score < base.score


@pytest.mark.parametrize(
    "config",
    [{"disable": ["RES999"]}, {"severity": {"RES001": "critical"}}, {"severity": {"NOPE": "low"}}, {"ignore": ["RES001"]}],
)
def test_bad_config_is_an_error_not_a_silent_no_op(config):
    with pytest.raises(ValueError):
        assess([], config=config)


def test_suppression_wildcard():
    d = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "w", "annotations": {"resilience.kubeastra.io/ignore": "*"}},
        "spec": {"replicas": 1, "template": {"metadata": {"labels": {"a": "b"}}, "spec": {"containers": [{"name": "c", "image": "nginx"}]}}},
    }
    report = assess([d])
    assert report.findings and all(f.suppressed for f in report.findings)
    assert report.score == 100


def test_catalogue_and_rule_functions_agree():
    cat = load_rules()
    for rid, meta in cat["rules"].items():
        assert meta["title"] and meta["group"] and meta["severity"] in ("low", "medium", "high"), rid
