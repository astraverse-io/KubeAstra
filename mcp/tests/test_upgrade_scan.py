"""Phase 1: building a ClusterSnapshot — static manifest scan + live builder +
the generator's pluto parser. All keyless, no cluster. See PILOTS_PLAN.md §Phase 1.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from services.upgrade import scan_manifests, snapshot_from_cluster  # noqa: E402

INGRESS = """apiVersion: networking.k8s.io/v1beta1
kind: Ingress
metadata:
  name: web
  namespace: shop
"""
CRONJOB_HELM = """apiVersion: batch/v1beta1
kind: CronJob
metadata:
  name: nightly
  namespace: ops
  labels:
    app.kubernetes.io/managed-by: Helm
    helm.sh/chart: cron-1.2.3
"""
POD = """apiVersion: v1
kind: Pod
metadata: {name: p, namespace: default}
"""


def test_scan_manifests_directory(tmp_path):
    (tmp_path / "ing.yaml").write_text(INGRESS)
    (tmp_path / "cron.yaml").write_text(CRONJOB_HELM)
    (tmp_path / "multi.yaml").write_text(INGRESS + "---\n" + POD)
    (tmp_path / "notes.txt").write_text("ignored")  # non-manifest
    (tmp_path / "bad.yaml").write_text("{{ not: valid ]]]")  # unparseable -> skipped

    snap = scan_manifests(tmp_path)
    assert snap.source_mode == "static"
    gvks = {o.gvk for o in snap.objects}
    assert {"networking.k8s.io/v1beta1/Ingress", "batch/v1beta1/CronJob", "v1/Pod"} <= gvks

    cron = next(o for o in snap.objects if o.kind == "CronJob")
    assert cron.source == "helm"  # detected from labels
    ing = next(o for o in snap.objects if o.kind == "Ingress" and o.namespace == "shop")
    assert ing.source == "manifest"


def test_scan_single_file(tmp_path):
    f = tmp_path / "ing.yaml"
    f.write_text(INGRESS)
    snap = scan_manifests(f)
    assert len(snap.objects) == 1 and snap.objects[0].kind == "Ingress"


def test_snapshot_from_cluster_builder():
    docs = [
        json.loads(
            '{"apiVersion":"policy/v1beta1","kind":"PodDisruptionBudget",'
            '"metadata":{"name":"pdb","namespace":"a"}}'
        )
    ]
    snap = snapshot_from_cluster(
        cluster_version="1.24",
        node_kubelet_versions=["1.24.3", "1.23.9"],
        provider="eks",
        object_docs=docs,
        helm_releases=[{"name": "r", "namespace": "a", "chart": "c", "chart_version": "1.0.0", "app_version": "2.0"}],
        operators=[{"name": "cert-manager", "version": "1.13.0", "crds": ["cert-manager.io"]}],
    )
    assert snap.source_mode == "live" and snap.provider == "eks"
    assert snap.objects[0].kind == "PodDisruptionBudget"
    assert snap.helm_releases[0].chart_version == "1.0.0"
    assert snap.operators[0].crds == ["cert-manager.io"]


def test_generator_parse_pluto_maps_and_filters():
    gen_path = MCP_DIR / "scripts" / "gen_api_deprecations.py"
    spec = importlib.util.spec_from_file_location("gen_api_deprecations", gen_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    sample = {
        "deprecated-versions": [
            {"version": "networking.k8s.io/v1beta1", "kind": "Ingress", "deprecated-in": "v1.19.0",
             "removed-in": "v1.22.0", "replacement-api": "networking.k8s.io/v1", "component": "k8s"},
            {"version": "policy/v1beta1", "kind": "PodSecurityPolicy", "deprecated-in": "v1.21.0",
             "removed-in": "v1.25.0", "replacement-api": "", "component": "k8s"},
            # deprecated but not removed -> skipped (our schema is about breakage)
            {"version": "apps/v1beta1", "kind": "Deployment", "deprecated-in": "v1.9.0",
             "removed-in": "", "component": "k8s"},
            # non-k8s component -> skipped
            {"version": "foo/v1", "kind": "Bar", "removed-in": "v1.20.0", "component": "istio"},
        ]
    }
    rows = mod.parse_pluto(sample)
    assert {r["kind"] for r in rows} == {"Ingress", "PodSecurityPolicy"}
    psp = next(r for r in rows if r["kind"] == "PodSecurityPolicy")
    assert psp["removed_in"] == "1.25" and psp["replacement"] is None
    ing = next(r for r in rows if r["kind"] == "Ingress")
    assert ing["group"] == "networking.k8s.io" and ing["version"] == "v1beta1"


def _load_generator():
    gen_path = MCP_DIR / "scripts" / "gen_api_deprecations.py"
    spec = importlib.util.spec_from_file_location("gen_api_deprecations", gen_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generator_fetch_rejects_non_https():
    # Review fix #2: _fetch must refuse file:// / http:// (SSRF / local-file guard).
    mod = _load_generator()
    for bad in ("file:///etc/passwd", "http://example.com/x", "ftp://host/x"):
        with pytest.raises(ValueError):
            mod._fetch(bad)


# ── live scan adapter (injected kubectl runner, no cluster) ───────────────────

from services.upgrade import scan_cluster  # noqa: E402
from services.upgrade.maps import Deprecation  # noqa: E402

_DEPS = [
    Deprecation(kind="Ingress", group="networking.k8s.io", version="v1beta1",
                removed_in="1.22", replacement="networking.k8s.io/v1"),
]


def test_scan_cluster_builds_snapshot_from_injected_runner():
    responses = {
        ("version", "-o", "json"): {"serverVersion": {"gitVersion": "v1.21.5"}},
        ("get", "nodes", "-o", "json"): {
            "items": [
                {"status": {"nodeInfo": {"kubeletVersion": "v1.21.5"}},
                 "spec": {"providerID": "aws:///us-east-1a/i-abc"}}
            ]
        },
        ("get", "crds", "-o", "json"): {"items": [{"spec": {"group": "cert-manager.io"}}]},
        ("get", "Ingress.v1beta1.networking.k8s.io", "-A", "-o", "json"): {
            "items": [{"apiVersion": "networking.k8s.io/v1beta1", "kind": "Ingress",
                       "metadata": {"name": "web", "namespace": "shop"}}]
        },
    }
    seen = []

    def fake(args):
        seen.append(tuple(args))
        return responses.get(tuple(args), {})

    snap = scan_cluster(fake, _DEPS)
    assert snap.source_mode == "live"
    assert snap.cluster_version == "1.21.5"  # 'v' stripped
    assert snap.provider == "eks"  # from aws:// providerID
    assert snap.node_kubelet_versions == ["v1.21.5"]
    # CodeQL py/incomplete-url-substring-sanitization false positive: `o.crds` is a
    # list of detected CRD group names, and this is list-membership — not URL parsing
    # or a security check.
    assert any("cert-manager.io" in o.crds for o in snap.operators)
    ing = [o for o in snap.objects if o.kind == "Ingress"]
    assert ing and ing[0].namespace == "shop"
    # probed the deprecated GVK by Kind.version.group
    assert ("get", "Ingress.v1beta1.networking.k8s.io", "-A", "-o", "json") in seen


def test_scan_cluster_tolerates_runner_errors():
    def boom(args):
        raise RuntimeError("no cluster reachable")

    snap = scan_cluster(boom, _DEPS)
    assert snap.source_mode == "live"
    assert snap.objects == [] and snap.cluster_version == "" and snap.provider == "unknown"
