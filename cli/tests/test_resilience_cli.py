"""Resilience Pilot Phase 1: the keyless ``kubeastra resilience`` command.

Exercises ``kubeastra.resilience`` directly (no backend, no API key, no
cluster) against the golden fixture app in mcp/tests/fixtures/resilience/, plus
the Typer wiring for exit codes. Live mode uses an injected kubectl runner.
"""
import json
import sys
from pathlib import Path

import pytest

CLI_SRC = Path(__file__).resolve().parents[1] / "src"
MCP_DIR = Path(__file__).resolve().parents[2] / "mcp"
for _p in (str(CLI_SRC), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from typer.testing import CliRunner  # noqa: E402

from kubeastra.cli import app  # noqa: E402
from kubeastra.resilience import ResilienceUsageError, load_resilience_core, run_assess  # noqa: E402

PAYMENTS = str(MCP_DIR / "tests" / "fixtures" / "resilience" / "payments")
HEALTHY = """apiVersion: apps/v1
kind: Deployment
metadata: {name: web, namespace: shop}
spec:
  replicas: 1
  selector: {matchLabels: {app: web}}
  template:
    metadata: {labels: {app: web}}
    spec:
      containers:
        - name: app
          image: nginx:1.27.2
          resources: {requests: {cpu: 100m, memory: 64Mi}, limits: {memory: 128Mi}}
"""


@pytest.fixture
def healthy(tmp_path):
    d = tmp_path / "healthy"
    d.mkdir()
    (d / "web.yaml").write_text(HEALTHY)
    return str(d)


def test_text_report_without_a_gate_exits_zero():
    code, text = run_assess(PAYMENTS)
    assert code == 0
    assert "Resilience score: 77/100 (C)" in text
    assert "RES001 high" in text and "(payments.yaml:3)" in text
    assert "Suppressed (1): RES011 Deployment shop/ledger" in text
    assert "Gate:" not in text


@pytest.mark.parametrize(
    "kw,code",
    [
        ({"fail_under": 80}, 1),
        ({"fail_under": 77}, 0),
        ({"fail_on": "high"}, 1),
        ({"fail_on": "low"}, 1),
    ],
)
def test_gates(kw, code):
    got, text = run_assess(PAYMENTS, **kw)
    assert got == code
    assert ("Gate: FAIL" in text) == (code == 1)


def test_fail_on_ignores_advisory_and_suppressed(tmp_path):
    # Only an advisory (orphan PDB, static) and a suppressed finding: nothing scored.
    d = tmp_path / "m"
    d.mkdir()
    (d / "x.yaml").write_text(
        HEALTHY.replace("metadata: {name: web, namespace: shop}",
                        "metadata: {name: web, namespace: shop, annotations: {resilience.kubeastra.io/ignore: '*'}}")
        + "---\napiVersion: policy/v1\nkind: PodDisruptionBudget\nmetadata: {name: ghost, namespace: shop}\n"
          "spec: {maxUnavailable: 1, selector: {matchLabels: {app: ghost}}}\n"
    )
    code, _ = run_assess(str(d), fail_on="low")
    assert code == 0


def test_json_output_carries_the_gate():
    code, text = run_assess(PAYMENTS, output="json", fail_on="high")
    data = json.loads(text)
    assert code == 1
    assert data["score"] == 77 and data["grade"] == "C"
    assert data["gate"] == {"enabled": True, "passed": False, "reasons": ["2 finding(s) at or above --fail-on high"]}


def test_sarif_locations_levels_and_suppressions(monkeypatch):
    monkeypatch.chdir(MCP_DIR.parent)  # SARIF URIs are repo-relative, as code scanning needs
    _, text = run_assess(PAYMENTS, output="sarif")
    run = json.loads(text)["runs"][0]
    assert {r["id"] for r in run["tool"]["driver"]["rules"]} == set(load_resilience_core().RULES)
    by_rule = {}
    for r in run["results"]:
        by_rule.setdefault(r["ruleId"], []).append(r)
    res001 = by_rule["RES001"][0]
    assert res001["level"] == "error"
    loc = res001["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == "mcp/tests/fixtures/resilience/payments/payments.yaml"
    assert loc["region"]["startLine"] == 3
    assert by_rule["RES014"][0]["level"] == "note"  # advisory
    assert by_rule["RES011"][0]["suppressions"][0]["kind"] == "inSource"


def test_repo_config_is_discovered_and_applied(tmp_path):
    (tmp_path / ".kubeastra").mkdir()
    (tmp_path / ".kubeastra" / "resilience.yaml").write_text("disable: [RES001]\nseverity: {RES010: high}\n")
    _, text = run_assess(PAYMENTS, output="json", cwd=tmp_path)
    rules = {f["rule"]: f["severity"] for f in json.loads(text)["findings"]}
    assert "RES001" not in rules and rules["RES010"] == "high"


@pytest.mark.parametrize(
    "kw,match",
    [
        ({"path": None}, "PATH .* or --live"),
        ({"path": PAYMENTS, "live": True}, "not both"),
        ({"path": "/no/such/dir"}, "path not found"),
        ({"path": PAYMENTS, "output": "xml"}, "--output"),
        ({"path": PAYMENTS, "fail_on": "critical"}, "--fail-on"),
        ({"path": PAYMENTS, "fail_under": 101}, "--fail-under"),
        ({"path": PAYMENTS, "config_path": "/no/such.yaml"}, "config file not found"),
    ],
)
def test_usage_errors(kw, match):
    path = kw.pop("path")
    with pytest.raises(ResilienceUsageError, match=match):
        run_assess(path, **kw)


def test_empty_directory_is_an_error_not_a_pass(tmp_path):
    (tmp_path / "README.md").write_text("# not manifests\n")
    with pytest.raises(ResilienceUsageError, match="no Kubernetes manifests"):
        run_assess(str(tmp_path), fail_under=90)


def test_bad_config_rule_is_a_usage_error(tmp_path):
    cfg = tmp_path / "r.yaml"
    cfg.write_text("disable: [RES999]\n")
    with pytest.raises(ResilienceUsageError, match="RES999"):
        run_assess(PAYMENTS, config_path=str(cfg))


def test_live_mode_uses_kubectl_objects_and_node_facts():
    deploy = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "api", "namespace": "prod"},
              "spec": {"replicas": 3, "template": {"metadata": {"labels": {"app": "api"}},
                       "spec": {"containers": [{"name": "c", "image": "api:1.0.0"}]}}}}
    calls = []

    def kubectl(args):
        calls.append(args)
        if args[:2] == ["get", "nodes"]:
            return {"items": [{"metadata": {"labels": {"topology.kubernetes.io/zone": z}}, "spec": {}} for z in "abc"]}
        return {"apiVersion": "v1", "kind": "List", "items": [deploy]}

    _, text = run_assess(live=True, run_kubectl=kubectl, output="json")
    data = json.loads(text)
    assert data["source_mode"] == "live" and data["facts"] == {"nodes": 3, "zones": ["a", "b", "c"]}
    res005 = next(f for f in data["findings"] if f["rule"] == "RES005")
    assert "3 zones" in res005["message"]
    assert calls[0][1] == load_resilience_core().LIVE_KINDS and "--all-namespaces" in calls[0]


def test_live_mode_kubectl_failure_is_an_error(monkeypatch):
    def boom(args):
        raise ResilienceUsageError("`kubectl get nodes` failed: Unauthorized")

    with pytest.raises(ResilienceUsageError, match="Unauthorized"):
        run_assess(live=True, run_kubectl=boom)


# ── Typer wiring: exit codes are the CI contract ──────────────────────────────

runner = CliRunner()


def test_cli_exit_codes(healthy):
    assert runner.invoke(app, ["resilience", PAYMENTS]).exit_code == 0
    assert runner.invoke(app, ["resilience", PAYMENTS, "--fail-on", "high"]).exit_code == 1
    assert runner.invoke(app, ["resilience", healthy, "--fail-under", "90"]).exit_code == 0
    bad = runner.invoke(app, ["resilience"])
    assert bad.exit_code == 2


def test_cli_help_lists_resilience():
    assert "resilience" in runner.invoke(app, ["--help"]).output
