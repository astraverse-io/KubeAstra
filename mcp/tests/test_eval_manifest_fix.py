"""Manifest-fix eval core: validators + golden-dataset invariants (design §7).

The validators are pure/deterministic, so these run offline. The dataset
invariants are the spine of the suite's trustworthiness: every golden fix must
score a perfect 1.0, and every broken manifest must score < 1.0 (otherwise the
case doesn't actually test a fix).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from evals.manifest_fix import validators  # noqa: E402
from evals.manifest_fix.types import Case, Check, load_cases  # noqa: E402


# ── validator units ─────────────────────────────────────────────────────────


def test_parse_manifest_returns_first_mapping():
    doc = validators.parse_manifest("apiVersion: v1\nkind: Service\nmetadata:\n  name: x\n")
    assert doc["kind"] == "Service"


def test_parse_manifest_bad_yaml_returns_none():
    assert validators.parse_manifest("key: [unclosed\n  : bad") is None


def test_resolve_path_with_list_index():
    doc = {"spec": {"containers": [{"image": "nginx:1.25.3"}]}}
    assert validators.resolve_path(doc, "spec.containers[0].image") == "nginx:1.25.3"


def test_resolve_path_missing_returns_none():
    doc = {"spec": {}}
    assert validators.resolve_path(doc, "spec.template.spec") is None
    assert validators.resolve_path(doc, "spec.containers[3].image") is None


def test_is_schema_valid_requires_core_fields():
    assert validators.is_schema_valid(
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "x"}}
    )
    assert not validators.is_schema_valid({"kind": "Service", "metadata": {"name": "x"}})
    assert not validators.is_schema_valid({"apiVersion": "v1", "kind": "Service", "metadata": {}})


def test_is_schema_valid_workload_needs_containers():
    base = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "web"},
        "spec": {"template": {"spec": {"containers": [{"name": "web", "image": "nginx"}]}}},
    }
    assert validators.is_schema_valid(base)
    # Drop the container image → invalid.
    base["spec"]["template"]["spec"]["containers"][0].pop("image")
    assert not validators.is_schema_valid(base)


def test_check_passes_equals_and_exists():
    doc = {"spec": {"replicas": 3, "x": {"y": "z"}}}
    assert validators.check_passes(doc, Check(path="spec.replicas", equals=3))
    assert not validators.check_passes(doc, Check(path="spec.replicas", equals=1))
    assert validators.check_passes(doc, Check(path="spec.x.y", exists=True))
    assert validators.check_passes(doc, Check(path="spec.missing", exists=False))


def test_score_unparseable_is_zero():
    case = Case(id="t", symptom="s", kind="Deployment", broken="", fixed="",
                checks=(Check(path="apiVersion", equals="apps/v1"),))
    result = validators.score_fix("{{ not yaml", case)
    assert result.score == 0.0
    assert result.parses is False


def test_score_partial_credit():
    # Schema-valid Service but the one check fails → 1 of 2 components → 0.5.
    case = Case(
        id="t", symptom="s", kind="Service", broken="", fixed="",
        checks=(Check(path="spec.ports[0].targetPort", equals=80),),
    )
    candidate = (
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: web\n"
        "spec:\n  ports:\n    - targetPort: 8080\n"
    )
    result = validators.score_fix(candidate, case)
    assert result.parses is True
    assert result.schema_valid is True
    assert result.checks_passed == 0
    assert result.score == pytest.approx(0.5)


# ── golden-dataset invariants ────────────────────────────────────────────────


def test_dataset_loads():
    cases = load_cases()
    assert len(cases) >= 8
    assert len({c.id for c in cases}) == len(cases)  # unique ids
    for c in cases:
        assert c.checks, f"{c.id} has no checks — it can't score a fix"


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c.id)
def test_golden_fix_scores_perfect(case: Case):
    result = validators.score_fix(case.fixed, case)
    assert result.score == 1.0, f"{case.id}: golden fix scored {result.score} ({result.detail})"


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c.id)
def test_broken_manifest_scores_below_one(case: Case):
    result = validators.score_fix(case.broken, case)
    assert result.score < 1.0, f"{case.id}: broken manifest already scores 1.0 — case is a no-op"
