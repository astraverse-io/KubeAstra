"""Resilience Pilot Phase 0: every rule fires on its pattern and stays quiet on
the fixed version. See internal_docs/features/RESILIENCE_PILOT_PLAN.md §4.

Objects are built with small dict helpers (rather than YAML strings) because
most cases are one-field variations of the same Deployment.
"""
import copy
import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from services.resilience import RULES, ClusterFacts, assess  # noqa: E402

GOOD_CONTAINER = {
    "name": "app",
    "image": "ghcr.io/acme/app:1.4.2",
    "ports": [{"containerPort": 8080}],
    "readinessProbe": {"httpGet": {"path": "/ready", "port": 8080}},
    "livenessProbe": {"httpGet": {"path": "/live", "port": 8080}},
    "lifecycle": {"preStop": {"exec": {"command": ["sleep", "5"]}}},
    "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "256Mi"}},
}

SPREAD = [{"maxSkew": 1, "topologyKey": "kubernetes.io/hostname", "whenUnsatisfiable": "DoNotSchedule",
           "labelSelector": {"matchLabels": {"app": "web"}}}]


def deploy(name="web", replicas=3, labels=None, container=None, pod_extra=None, spec_extra=None, kind="Deployment", ns="shop", annotations=None):
    labels = labels or {"app": name}
    c = copy.deepcopy(container if container is not None else GOOD_CONTAINER)
    pod_spec = {"containers": [c], "topologySpreadConstraints": copy.deepcopy(SPREAD)}
    pod_spec.update(pod_extra or {})
    spec = {"replicas": replicas, "selector": {"matchLabels": labels},
            "template": {"metadata": {"labels": labels}, "spec": pod_spec}}
    spec.update(spec_extra or {})
    meta = {"name": name, "namespace": ns}
    if annotations:
        meta["annotations"] = annotations
    return {"apiVersion": "apps/v1", "kind": kind, "metadata": meta, "spec": spec}


def svc(name="web", selector=None, ns="shop", **spec):
    s = {"selector": selector if selector is not None else {"app": name}, "ports": [{"port": 80}]}
    s.update(spec)
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": ns}, "spec": s}


def pdb(name="web", ns="shop", selector=None, **spec):
    s = {"selector": selector if selector is not None else {"matchLabels": {"app": "web"}}}
    s.update(spec)
    return {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {"name": name, "namespace": ns}, "spec": s}


def hpa(target="web", ns="shop", api="autoscaling/v2", **spec):
    s = {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": target}, "maxReplicas": 10}
    s.update(spec)
    return {"apiVersion": api, "kind": "HorizontalPodAutoscaler", "metadata": {"name": target, "namespace": ns}, "spec": s}


def with_container(**changes):
    c = copy.deepcopy(GOOD_CONTAINER)
    for k, v in changes.items():
        if v is None:
            c.pop(k, None)
        else:
            c[k] = v
    return c


CPU_UTIL = [{"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 70}}}]

# The healthy baseline: no rule may fire on it.
HEALTHY = [deploy(), svc(), pdb(maxUnavailable=1), hpa(minReplicas=3, metrics=CPU_UTIL)]

# (rule, docs that must fire it, docs that must not)
CASES = [
    ("RES001", [deploy(replicas=2), svc(), pdb(minAvailable=2)], [deploy(replicas=2), svc(), pdb(minAvailable=1)]),
    ("RES001", [deploy(replicas=4), svc(), pdb(minAvailable="100%")], [deploy(replicas=4), svc(), pdb(minAvailable="75%")]),
    ("RES001", [deploy(replicas=3), svc(), pdb(maxUnavailable=0)], [deploy(replicas=3), svc(), pdb(maxUnavailable="10%")]),
    # HPA floor is what a drain at low traffic sees: replicas=5 doesn't save a minAvailable=2 / minReplicas=2 pair.
    ("RES001", [deploy(replicas=5), svc(), pdb(minAvailable=2), hpa(minReplicas=2, metrics=CPU_UTIL)],
               [deploy(replicas=5), svc(), pdb(minAvailable=2), hpa(minReplicas=3, metrics=CPU_UTIL)]),
    ("RES002", [deploy(replicas=1, pod_extra={"topologySpreadConstraints": []}), svc()],
               [deploy(replicas=1, pod_extra={"topologySpreadConstraints": []}), svc(), pdb(maxUnavailable=1)]),
    ("RES003", [deploy(container=with_container(resources={"limits": {"memory": "1Gi"}})), svc(), pdb(maxUnavailable=1),
                hpa(minReplicas=3, metrics=CPU_UTIL)], HEALTHY),
    ("RES003", [deploy(container=with_container(resources={"requests": {"memory": "1Gi"}})), svc(), pdb(maxUnavailable=1),
                hpa(api="autoscaling/v1", minReplicas=3, targetCPUUtilizationPercentage=60)],
               [deploy(), svc(), pdb(maxUnavailable=1), hpa(api="autoscaling/v1", minReplicas=3, targetCPUUtilizationPercentage=60)]),
    ("RES004", [deploy(), svc(), pdb(maxUnavailable=1), hpa(metrics=CPU_UTIL)], HEALTHY),
    ("RES005", [deploy(pod_extra={"topologySpreadConstraints": []}), svc(), pdb(maxUnavailable=1)],
               [deploy(pod_extra={"topologySpreadConstraints": [], "affinity": {"podAntiAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": []}}}),
                svc(), pdb(maxUnavailable=1)]),
    ("RES006", [deploy(container=with_container(readinessProbe=None)), svc(), pdb(maxUnavailable=1)], HEALTHY),
    ("RES007", [deploy(container=with_container(livenessProbe=GOOD_CONTAINER["readinessProbe"])), svc(), pdb(maxUnavailable=1)], HEALTHY),
    ("RES008", [deploy(container=with_container(lifecycle=None)), svc(), pdb(maxUnavailable=1)], HEALTHY),
    ("RES009", [deploy(pod_extra={"terminationGracePeriodSeconds": 10},
                       container=with_container(lifecycle={"preStop": {"exec": {"command": ["/bin/sh", "-c", "sleep 15"]}}})),
                svc(), pdb(maxUnavailable=1)],
               [deploy(pod_extra={"terminationGracePeriodSeconds": 30},
                       container=with_container(lifecycle={"preStop": {"exec": {"command": ["/bin/sh", "-c", "sleep 15"]}}})),
                svc(), pdb(maxUnavailable=1)]),
    ("RES009", [deploy(container=with_container(lifecycle={"preStop": {"sleep": {"seconds": 30}}})), svc(), pdb(maxUnavailable=1)],
               [deploy(container=with_container(lifecycle={"preStop": {"sleep": {"seconds": 20}}})), svc(), pdb(maxUnavailable=1)]),
    ("RES010", [deploy(container=with_container(resources={"requests": {"cpu": "1"}})), svc(), pdb(maxUnavailable=1)], HEALTHY),
    ("RES011", [deploy(container=with_container(image="nginx")), svc(), pdb(maxUnavailable=1)], HEALTHY),
    ("RES011", [deploy(container=with_container(image="registry.local:5000/app:latest")), svc(), pdb(maxUnavailable=1)],
               [deploy(container=with_container(image="registry.local:5000/app@sha256:abc")), svc(), pdb(maxUnavailable=1)]),
    ("RES012", [deploy(spec_extra={"strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": "100%"}}}), svc(), pdb(maxUnavailable=1)],
               [deploy(spec_extra={"strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": "50%"}}}), svc(), pdb(maxUnavailable=1)]),
    ("RES012", [deploy(replicas=3, spec_extra={"strategy": {"rollingUpdate": {"maxUnavailable": 3}}}), svc(), pdb(maxUnavailable=1)],
               [deploy(replicas=3, spec_extra={"strategy": {"type": "Recreate"}}), svc(), pdb(maxUnavailable=1)]),
    ("RES013", HEALTHY + [svc(name="ghost", selector={"app": "ghost"})], HEALTHY + [svc(name="ext", selector={}, type="ExternalName", externalName="x.example")]),
    ("RES014", HEALTHY + [pdb(name="ghost", selector={"matchLabels": {"app": "ghost"}}, maxUnavailable=1)], HEALTHY),
]


def _rules(docs, **kw):
    return {f.rule for f in assess(docs, **kw).findings}


def test_healthy_baseline_is_clean():
    report = assess(HEALTHY)
    assert report.findings == []
    assert report.score == 100 and report.grade == "A"


def test_every_rule_has_a_case():
    assert {r for r, _, _ in CASES} == set(RULES)


@pytest.mark.parametrize("rule,fires,clean", CASES, ids=[f"{c[0]}-{i}" for i, c in enumerate(CASES)])
def test_rule_fires_and_stays_quiet(rule, fires, clean):
    assert rule in _rules(fires), f"{rule} should fire"
    assert rule not in _rules(clean), f"{rule} should not fire on the fixed version"


# ── edge cases the table doesn't make obvious ─────────────────────────────────

def test_res001_lists_every_object_involved():
    f = next(f for f in assess([deploy(replicas=2), svc(), pdb(minAvailable=2)]).findings if f.rule == "RES001")
    assert [r.kind for r in f.objects] == ["Deployment", "PodDisruptionBudget"]
    assert "minAvailable=2 but replicas=2" in f.message


def test_res001_counts_pods_across_every_workload_the_pdb_covers():
    a = deploy(name="a", replicas=1, labels={"tier": "api", "app": "a"})
    b = deploy(name="b", replicas=1, labels={"tier": "api", "app": "b"})
    shared = pdb(selector={"matchLabels": {"tier": "api"}}, minAvailable=1)
    assert "RES001" not in _rules([a, b, shared])  # 2 pods, 1 must stay: one can go
    assert "RES001" in _rules([a, b, pdb(selector={"matchLabels": {"tier": "api"}}, minAvailable=2)])


def test_res001_ignores_daemonsets_and_scaled_to_zero():
    ds = deploy(kind="DaemonSet")
    ds["spec"].pop("replicas")
    assert "RES001" not in _rules([ds, pdb(minAvailable=5)])
    assert "RES001" not in _rules([deploy(replicas=0), pdb(minAvailable=1)])


def test_pdb_in_another_namespace_does_not_cover_the_workload():
    docs = [deploy(replicas=1, ns="shop", pod_extra={"topologySpreadConstraints": []}), svc(ns="shop"), pdb(ns="other", maxUnavailable=1)]
    assert "RES002" in _rules(docs)


def test_static_manifests_without_namespace_link_to_anything():
    d = deploy(replicas=2)
    del d["metadata"]["namespace"]
    p = pdb(minAvailable=2)
    del p["metadata"]["namespace"]
    assert "RES001" in _rules([d, p])


def test_res003_v2_without_metrics_defaults_to_cpu():
    docs = [deploy(container=with_container(resources={"requests": {"memory": "1Gi"}, "limits": {"memory": "1Gi"}})), hpa(minReplicas=3)]
    assert "RES003" in _rules(docs)


def test_res003_container_resource_only_checks_that_container():
    sidecar = {"name": "proxy", "image": "envoy:1.30.1"}  # no requests
    d = deploy()
    d["spec"]["template"]["spec"]["containers"].append(sidecar)
    m = [{"type": "ContainerResource", "containerResource": {"name": "cpu", "container": "app", "target": {"type": "Utilization", "averageUtilization": 60}}}]
    assert "RES003" not in _rules([d, hpa(minReplicas=3, metrics=m)])
    m[0]["containerResource"]["container"] = "proxy"
    assert "RES003" in _rules([d, hpa(minReplicas=3, metrics=m)])


def test_res003_average_value_targets_need_no_request():
    m = [{"type": "Resource", "resource": {"name": "memory", "target": {"type": "AverageValue", "averageValue": "500Mi"}}}]
    d = deploy(container=with_container(resources={"requests": {"cpu": "1"}, "limits": {"memory": "1Gi"}}))
    assert "RES003" not in _rules([d, hpa(minReplicas=3, metrics=m)])


def test_res005_live_mode_single_node_is_not_a_finding_and_zones_are_named():
    docs = [deploy(pod_extra={"topologySpreadConstraints": []})]
    assert "RES005" not in _rules(docs, source_mode="live", facts=ClusterFacts(nodes=1, zones=["a"]))
    f = next(f for f in assess(docs, source_mode="live", facts=ClusterFacts(nodes=6, zones=["a", "b", "c"])).findings if f.rule == "RES005")
    assert "3 zones" in f.message


def test_wiring_findings_are_advisory_only_in_static_mode():
    docs = HEALTHY + [svc(name="ghost", selector={"app": "ghost"})]
    static = next(f for f in assess(docs).findings if f.rule == "RES013")
    live = next(f for f in assess(docs, source_mode="live").findings if f.rule == "RES013")
    assert static.advisory and not static.scored
    assert not live.advisory


def test_kubectl_list_documents_are_expanded():
    lst = {"apiVersion": "v1", "kind": "List", "items": [deploy(replicas=2), pdb(minAvailable=2)]}
    assert "RES001" in _rules([lst])


def test_jobs_get_hygiene_findings_only():
    job = {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "nightly", "namespace": "ops"},
           "spec": {"schedule": "0 3 * * *", "jobTemplate": {"spec": {"template": {"metadata": {"labels": {"app": "nightly"}},
                    "spec": {"containers": [{"name": "run", "image": "busybox"}]}}}}}}
    assert _rules([job]) == {"RES010", "RES011"}


def test_facts_from_nodes_counts_schedulable_nodes_and_zones():
    from services.resilience import facts_from_nodes

    def node(zone=None, cordoned=False):
        labels = {"topology.kubernetes.io/zone": zone} if zone else {}
        return {"metadata": {"labels": labels}, "spec": {"unschedulable": True} if cordoned else {}}

    f = facts_from_nodes({"items": [node("a"), node("b"), node("b"), node("c", cordoned=True)]})
    assert f.nodes == 3 and f.zones == ["a", "b"]
    assert facts_from_nodes({"items": [node(), node()]}).zones is None  # kind / bare metal: no zone labels
    assert facts_from_nodes({}).nodes is None  # kubectl output we can't read: unknown, not zero


def test_metrics_only_services_do_not_make_a_workload_serving():
    """Found by the corpus sweep: a Service whose ports are all metrics ports is
    a Prometheus scrape target, not user traffic. RES002/RES006/RES008 must not
    fire because of it — but a Service with any real port still counts."""
    single = deploy(replicas=1, pod_extra={"topologySpreadConstraints": []},
                    container=with_container(readinessProbe=None, lifecycle=None))
    metrics = svc(ports=[{"name": "http-metrics", "port": 9402}])
    serving_rules = {"RES002", "RES006", "RES008"}
    assert not serving_rules & _rules([single, metrics])
    for name in ("metrics", "metrics-port", "http-monitoring", "tcp-prometheus"):
        assert not serving_rules & _rules([single, svc(ports=[{"name": name, "port": 9000}])]), name
    mixed = svc(ports=[{"name": "https", "port": 443}, {"name": "metrics", "port": 9402}])
    assert serving_rules <= _rules([single, mixed])
    keda_style = svc(ports=[{"name": "metricsservice", "port": 9666}])  # real gRPC traffic
    assert serving_rules <= _rules([single, keda_style])
