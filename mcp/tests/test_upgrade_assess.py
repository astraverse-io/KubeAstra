"""Phase 1: deterministic, keyless detection — assess() against the real maps.

Covers: removed-API detection (guaranteed), the removal-version boundary,
removed-with-no-replacement, dedupe, static-mode operator skip, live operator
bump hints, skew, and that no LLM is imported. See PILOTS_PLAN.md §Phase 1.
"""
import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from services.upgrade import (  # noqa: E402
    ClusterSnapshot,
    ObjectRef,
    OperatorInfo,
    assess,
    load_maps,
)

MAPS = load_maps()


def _obj(api_version, kind, name="x", ns="default", source="manifest"):
    return ObjectRef(gvk=f"{api_version}/{kind}", kind=kind, name=name, namespace=ns, source=source)


def test_detects_removed_api_as_blocking():
    snap = ClusterSnapshot(
        cluster_version="1.21",
        objects=[_obj("networking.k8s.io/v1beta1", "Ingress", "web", "shop"), _obj("v1", "Pod", "p")],
    )
    rep = assess(snap, "1.22", MAPS)
    assert len(rep.blocking) == 1
    b = rep.blocking[0]
    assert b.kind == "Ingress"
    assert b.replacement == "networking.k8s.io/v1"
    assert b.advisory is False
    assert b.gvk == "networking.k8s.io/v1beta1/Ingress"


def test_not_blocking_before_removal_but_noted_as_deprecated():
    snap = ClusterSnapshot(objects=[_obj("networking.k8s.io/v1beta1", "Ingress")])
    rep = assess(snap, "1.21", MAPS)  # removed in 1.22, so not yet a blocker at 1.21
    assert rep.blocking == []
    assert any("deprecated" in n.lower() for n in rep.notes)


def test_removed_with_no_replacement_is_null():
    snap = ClusterSnapshot(objects=[_obj("policy/v1beta1", "PodSecurityPolicy", "psp", "")])
    rep = assess(snap, "1.25", MAPS)
    assert rep.blocking and rep.blocking[0].replacement is None


def test_dedupes_identical_objects():
    snap = ClusterSnapshot(
        objects=[_obj("batch/v1beta1", "CronJob", "c", "ops"), _obj("batch/v1beta1", "CronJob", "c", "ops")]
    )
    rep = assess(snap, "1.25", MAPS)
    assert len(rep.blocking) == 1


def test_static_mode_detects_api_but_skips_operator_advice():
    snap = ClusterSnapshot(
        source_mode="static",
        objects=[_obj("batch/v1beta1", "CronJob")],
        operators=[OperatorInfo(name="cert-manager", version="1.0.0", crds=["cert-manager.io"])],
    )
    rep = assess(snap, "1.25", MAPS)
    assert rep.blocking  # API detection works in static mode
    assert rep.operators == []  # operator advice needs a live cluster
    assert any("Static mode" in n for n in rep.notes)


def test_operator_bump_hint_in_live_mode():
    snap = ClusterSnapshot(
        source_mode="live",
        cluster_version="1.28",
        operators=[OperatorInfo(name="cert-manager", version="1.9.0", crds=["cert-manager.io"])],
    )
    rep = assess(snap, "1.29", MAPS)
    cm = next((o for o in rep.operators if o.name == "cert-manager"), None)
    assert cm is not None
    assert cm.advisory is True
    assert cm.min_required == "1.13.0"  # the floor for k8s 1.29
    assert cm.action == "bump"  # installed 1.9.0 < 1.13.0


def test_skew_flagged_when_nodes_trail_too_far():
    snap = ClusterSnapshot(source_mode="live", node_kubelet_versions=["1.25.0"])
    rep = assess(snap, "1.31", MAPS)  # nodes 6 minors behind; policy allows 3
    assert rep.skew.ok is False


def test_assess_is_keyless_no_llm_import(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def guard(name, *a, **k):
        if name.split(".")[0] in {"anthropic", "openai", "google"}:
            raise AssertionError(f"assess tried to import an LLM client: {name}")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", guard)
    snap = ClusterSnapshot(objects=[_obj("batch/v1beta1", "CronJob")])
    assert assess(snap, "1.25", MAPS).blocking
