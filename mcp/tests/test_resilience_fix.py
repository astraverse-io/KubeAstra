"""Resilience Pilot Phase 2: fixes — one builder per rule, value forms kept,
nothing guessed, Helm-rendered objects never patched, and the --fix files.
See internal_docs/features/RESILIENCE_PILOT_PLAN.md §5.3.
"""
import copy
import sys
from pathlib import Path

import pytest
import yaml

MCP_DIR = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for _p in (str(MCP_DIR), str(TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.resilience import RULES, ClusterFacts, assess, render_fixes, scan_path  # noqa: E402
from services.resilience.fix import _has_path, _merge, _nest  # noqa: E402
from test_resilience_rules import CASES, deploy, hpa, pdb, svc, with_container  # noqa: E402

PAYMENTS = MCP_DIR / "tests" / "fixtures" / "resilience" / "payments"

EXPECTED_KIND = {
    "RES001": "field", "RES002": "new_object", "RES003": "advisory", "RES004": "field", "RES005": "block",
    "RES006": "advisory", "RES007": "advisory", "RES008": "block", "RES009": "field", "RES010": "advisory",
    "RES011": "advisory", "RES012": "field", "RES013": "advisory", "RES014": "advisory",
}


def _fix(rule, docs, **kw):
    return next(f.fix for f in assess(docs, **kw).findings if f.rule == rule)


def test_every_rule_has_a_builder_and_an_expected_kind():
    assert set(EXPECTED_KIND) == set(RULES)
    seen = {}
    for rule, fires, _ in CASES:
        seen.setdefault(rule, _fix(rule, fires).kind)
    assert seen == EXPECTED_KIND


@pytest.mark.parametrize(
    "replicas,pdb_spec,field,value",
    [
        (3, {"minAvailable": 3}, "minAvailable", 2),
        (4, {"minAvailable": "100%"}, "minAvailable", "75%"),
        (3, {"minAvailable": "100%"}, "minAvailable", "66%"),
        (3, {"maxUnavailable": 0}, "maxUnavailable", 1),
        (4, {"maxUnavailable": "0%"}, "maxUnavailable", "25%"),
    ],
)
def test_res001_value_keeps_its_form_and_unblocks_exactly_one_eviction(replicas, pdb_spec, field, value):
    fix = _fix("RES001", [deploy(replicas=replicas), svc(), pdb(**pdb_spec)])
    assert fix.field_path == ("spec", field) and fix.new_value == value
    assert type(fix.new_value) is type(pdb_spec[field])
    # and the fixed object no longer trips the rule
    fixed = pdb(**{field: value})
    assert "RES001" not in {f.rule for f in assess([deploy(replicas=replicas), svc(), fixed]).findings}


def test_res001_single_replica_is_advisory_not_a_pdb_that_protects_nothing():
    fix = _fix("RES001", [deploy(replicas=1), svc(), pdb(minAvailable=1)])
    assert fix.kind == "advisory" and "2 replicas" in fix.note


def test_res002_new_pdb_uses_the_workload_selector_and_says_replicas_are_not_touched():
    fix = _fix("RES002", [deploy(replicas=1, pod_extra={"topologySpreadConstraints": []}), svc()])
    obj = yaml.safe_load(fix.yaml)
    assert obj["kind"] == "PodDisruptionBudget" and obj["apiVersion"] == "policy/v1"
    assert obj["metadata"] == {"name": "web-pdb", "namespace": "shop"}
    assert obj["spec"] == {"maxUnavailable": 1, "selector": {"matchLabels": {"app": "web"}}}
    assert "doesn't change replicas" in fix.note


def test_res005_adds_a_zone_constraint_when_live_mode_sees_zones():
    docs = [deploy(pod_extra={"topologySpreadConstraints": []})]
    static = yaml.safe_load(_fix("RES005", docs).yaml)
    live = yaml.safe_load(_fix("RES005", docs, source_mode="live", facts=ClusterFacts(nodes=6, zones=["a", "b"])).yaml)
    keys = lambda d: [c["topologyKey"] for c in d["spec"]["template"]["spec"]["topologySpreadConstraints"]]  # noqa: E731
    assert keys(static) == ["kubernetes.io/hostname"]
    assert keys(live) == ["topology.kubernetes.io/zone", "kubernetes.io/hostname"]
    assert all(c["whenUnsatisfiable"] == "ScheduleAnyway" for c in live["spec"]["template"]["spec"]["topologySpreadConstraints"])


def test_res009_grace_outlasts_the_sleep():
    docs = [deploy(pod_extra={"terminationGracePeriodSeconds": 10},
                   container=with_container(lifecycle={"preStop": {"exec": {"command": ["sleep", "15"]}}})), svc()]
    fix = _fix("RES009", docs)
    assert fix.field_path == ("spec", "template", "spec", "terminationGracePeriodSeconds") and fix.new_value == 25
    assert fix.gitops is not None  # the field is in the source


@pytest.mark.parametrize(
    "replicas,strategy,kind,value",
    [
        (4, {"rollingUpdate": {"maxUnavailable": "100%"}}, "field", "25%"),
        (3, {"rollingUpdate": {"maxUnavailable": 3}}, "field", 1),
        (10, {"rollingUpdate": {"maxUnavailable": 10}}, "field", 3),
        (1, {"rollingUpdate": {"maxUnavailable": 1}}, "field", 0),
        (1, {"rollingUpdate": {"maxUnavailable": 1, "maxSurge": 0}}, "advisory", None),
    ],
)
def test_res012_rollout_values(replicas, strategy, kind, value):
    fix = _fix("RES012", [deploy(replicas=replicas, spec_extra={"strategy": strategy})])
    assert fix.kind == kind
    if kind == "field":
        assert fix.new_value == value


def test_advisory_fixes_never_carry_a_pr_or_a_guessed_value():
    for rule, fires, _ in CASES:
        fix = _fix(rule, fires)
        if fix.kind == "advisory":
            assert fix.gitops is None and fix.new_value is None, rule


def test_helm_rendered_objects_only_get_advisory_fixes():
    helm = {"helm.sh/chart": "web-1.0.0", "app.kubernetes.io/managed-by": "Helm"}
    for rule, fires, _ in CASES:
        docs = copy.deepcopy(fires)
        for d in docs:
            d["metadata"].setdefault("labels", {}).update(helm)
        fix = _fix(rule, docs)
        assert fix.kind == "advisory" and fix.gitops is None, rule
        if EXPECTED_KIND[rule] != "advisory":
            assert "web-1.0.0" in fix.note and fix.yaml, rule  # the change is still shown


def test_nest_and_has_path_address_containers_by_name():
    path = ("spec", "template", "spec", "containers", "app", "lifecycle")
    assert _nest(path, {"x": 1}) == {"spec": {"template": {"spec": {"containers": [{"name": "app", "lifecycle": {"x": 1}}]}}}}
    doc = deploy()
    assert _has_path(doc, ("spec", "template", "spec", "containers", "app", "readinessProbe"))
    assert not _has_path(doc, ("spec", "template", "spec", "containers", "nope", "readinessProbe"))
    assert not _has_path(doc, ("spec", "minReadySeconds"))


def test_merge_combines_container_changes_by_name():
    a = {"spec": {"template": {"spec": {"containers": [{"name": "app", "lifecycle": {"preStop": 1}}]}}}}
    b = {"spec": {"template": {"spec": {"containers": [{"name": "app", "readinessProbe": 2}, {"name": "side", "x": 3}]},
                                "topologySpreadConstraints": []}}}
    m = _merge(a, b)
    cs = m["spec"]["template"]["spec"]["containers"]
    assert cs == [{"name": "app", "lifecycle": {"preStop": 1}, "readinessProbe": 2}, {"name": "side", "x": 3}]


# ── --fix files ───────────────────────────────────────────────────────────────

def test_render_fixes_for_the_golden_app():
    files = render_fixes(assess(scan_path(PAYMENTS)))
    assert set(files) == {
        ".kubeastra-fixes", "README.md", "kustomization.yaml",
        "patches/deployment-shop-payments.yaml",
        "patches/poddisruptionbudget-shop-payments-pdb.yaml",
        "new/poddisruptionbudget-shop-ledger-pdb.yaml",
        "new/poddisruptionbudget-shop-redis-pdb.yaml",
    }
    pdb_patch = yaml.safe_load(files["patches/poddisruptionbudget-shop-payments-pdb.yaml"])
    assert pdb_patch == {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget",
                         "metadata": {"name": "payments-pdb", "namespace": "shop"}, "spec": {"minAvailable": 2}}
    kust = yaml.safe_load(files["kustomization.yaml"])
    assert kust["resources"] == ["new/poddisruptionbudget-shop-ledger-pdb.yaml", "new/poddisruptionbudget-shop-redis-pdb.yaml"]
    assert [p["path"] for p in kust["patches"]] == ["patches/deployment-shop-payments.yaml",
                                                     "patches/poddisruptionbudget-shop-payments-pdb.yaml"]
    readme = files["README.md"]
    assert "kubectl patch poddisruptionbudget payments-pdb -n shop --type strategic" in readme
    assert "## Needs you" in readme and "RES003" in readme
    assert "RES011" not in readme  # suppressed by annotation: no fix offered


def test_two_fixes_on_one_object_become_one_patch():
    d = deploy(pod_extra={"topologySpreadConstraints": []}, container=with_container(lifecycle=None))
    files = render_fixes(assess([d, svc(), pdb(maxUnavailable=1)]))
    patch = yaml.safe_load(files["patches/deployment-shop-web.yaml"])
    pod = patch["spec"]["template"]["spec"]
    assert pod["topologySpreadConstraints"] and pod["containers"][0]["lifecycle"]["preStop"]
    assert files["patches/deployment-shop-web.yaml"].startswith("# kubeastra resilience: RES005, RES008")


def test_nothing_to_fix():
    files = render_fixes(assess([deploy(), svc(), pdb(maxUnavailable=1), hpa(minReplicas=3, metrics=[
        {"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 70}}}])]))
    assert set(files) == {".kubeastra-fixes", "README.md"} and "Nothing to fix." in files["README.md"]
