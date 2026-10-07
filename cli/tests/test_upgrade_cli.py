"""Phase 3: the keyless ``kubeastra upgrade plan`` logic.

Tests the dependency-light ``kubeastra.upgrade`` module directly (no backend, no
API key, no cluster) — static-manifest mode. See PILOTS_PLAN.md §Phase 3.
"""
import json
import sys
from pathlib import Path

CLI_SRC = Path(__file__).resolve().parents[1] / "src"
MCP_DIR = Path(__file__).resolve().parents[2] / "mcp"
for _p in (str(CLI_SRC), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kubeastra.upgrade import load_upgrade_core, run_plan  # noqa: E402

INGRESS = "apiVersion: networking.k8s.io/v1beta1\nkind: Ingress\nmetadata: {name: web, namespace: shop}\n"
POD = "apiVersion: v1\nkind: Pod\nmetadata: {name: p}\n"


def _manifests(tmp_path, content):
    d = tmp_path / "m"
    d.mkdir(exist_ok=True)
    (d / "x.yaml").write_text(content)
    return str(d)


def test_text_output_flags_blocking_and_exits_nonzero(tmp_path):
    code, text = run_plan("1.22", manifests=_manifests(tmp_path, INGRESS), output="text")
    assert code == 1
    assert "networking.k8s.io/v1beta1/Ingress" in text
    assert "networking.k8s.io/v1" in text  # the replacement
    assert "Plan (" in text


def test_json_output(tmp_path):
    code, text = run_plan("1.22", manifests=_manifests(tmp_path, INGRESS), output="json")
    data = json.loads(text)
    assert code == 1
    assert data["report"]["blocking"] and data["plan"]["steps"]


def test_sarif_output(tmp_path):
    code, text = run_plan("1.22", manifests=_manifests(tmp_path, INGRESS), output="sarif")
    sarif = json.loads(text)
    assert sarif["version"] == "2.1.0"
    result = sarif["runs"][0]["results"][0]
    assert result["ruleId"] == "deprecated-api-removed" and result["level"] == "error"
    assert "1.22" in result["message"]["text"]


def test_clean_cluster_exits_zero(tmp_path):
    code, text = run_plan("1.31", manifests=_manifests(tmp_path, POD), output="text")
    assert code == 0 and "No blocking" in text


def test_fail_on_blocking_toggle(tmp_path):
    code, _ = run_plan("1.22", manifests=_manifests(tmp_path, INGRESS), output="text", fail_on_blocking=False)
    assert code == 0  # blocking found, but the CI gate is disabled


def test_load_upgrade_core_resolves_the_pure_core():
    core = load_upgrade_core()
    assert hasattr(core, "assess") and hasattr(core, "plan") and hasattr(core, "scan_manifests")
