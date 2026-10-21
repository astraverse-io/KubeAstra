"""The Resilience rules — one function per rule id, all deterministic.

A rule takes the linked :class:`~.model.Inventory` (plus what live mode knows)
and returns :class:`Hit`s. It never decides severity, title, suppression or
score — the engine (``engine.py``) does that from ``rules.yaml``, the repo
config and the annotations, so a rule stays a pure "does this pattern exist".

Kubernetes arithmetic the rules rely on (from the API docs):
- PDB ``minAvailable`` / ``maxUnavailable`` percentages round **up**.
- Deployment ``rollingUpdate.maxUnavailable`` percentages round **down**
  (default ``25%``).
- An HPA with no metrics (``autoscaling/v2``) or no
  ``targetCPUUtilizationPercentage`` (``autoscaling/v1``) targets 80% CPU.
- ``terminationGracePeriodSeconds`` defaults to 30.
- ``kubectl drain`` skips DaemonSet pods, so DaemonSets never block a drain.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .model import Inventory, Obj, Workload
from .types import ClusterFacts


@dataclass
class Ctx:
    inv: Inventory
    facts: ClusterFacts = field(default_factory=ClusterFacts)
    static: bool = True


@dataclass
class Hit:
    rule: str
    message: str
    objs: list[Obj]
    advisory: bool = False


def _int_or_pct(v: Any) -> Optional[tuple[str, float]]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return ("int", float(v))
    s = str(v).strip()
    if s.endswith("%"):
        try:
            return ("pct", float(s[:-1]))
        except ValueError:
            return None
    try:
        return ("int", float(int(s)))
    except ValueError:
        return None


def _who(ws: list[Workload]) -> str:
    if len(ws) == 1:
        w = ws[0]
        if w.hpa is not None:
            return f"HPA minReplicas={w.expected_replicas}"
        return f"replicas={w.expected_replicas}"
    return f"{sum(w.expected_replicas or 0 for w in ws)} pods across {len(ws)} workloads"


# ── drain ─────────────────────────────────────────────────────────────────────

def res001_pdb_blocks_eviction(ctx: Ctx) -> list[Hit]:
    hits = []
    for pdb in ctx.inv.pdbs:
        targets = [w for w in ctx.inv.pdb_targets.get(pdb.ref.key, []) if w.replicated]
        total = sum(w.expected_replicas or 0 for w in targets)
        if not targets or total <= 0:
            continue
        spec = pdb.spec
        why = None
        if "minAvailable" in spec:
            v = _int_or_pct(spec.get("minAvailable"))
            if v is not None:
                need = v[1] if v[0] == "int" else math.ceil(total * v[1] / 100)
                if need >= total:
                    why = f"minAvailable={spec['minAvailable']} but {_who(targets)}"
        elif "maxUnavailable" in spec:
            v = _int_or_pct(spec.get("maxUnavailable"))
            if v is not None and v[1] <= 0:
                why = f"maxUnavailable={spec['maxUnavailable']}"
        if why:
            hits.append(Hit("RES001", f"{why} — no pod can ever be evicted, so `kubectl drain` stalls on this node", [t.obj for t in targets] + [pdb]))
    return hits


def res002_single_replica_no_pdb(ctx: Ctx) -> list[Hit]:
    hits = []
    for w in ctx.inv.workloads:
        if w.replicated and w.expected_replicas == 1 and not w.pdbs and w.services:
            hits.append(
                Hit("RES002", "1 replica behind a Service and no PodDisruptionBudget — every node drain or eviction is downtime", [w.obj])
            )
    return hits


# ── scaling ───────────────────────────────────────────────────────────────────

def _hpa_resource_needs(hpa: Obj) -> list[tuple[str, Optional[str]]]:
    """``(resource, container-or-None)`` pairs the HPA computes a utilization for."""
    spec = hpa.spec
    api = str(hpa.doc.get("apiVersion", ""))
    if api == "autoscaling/v1":
        return [("cpu", None)]
    metrics = spec.get("metrics")
    if not metrics:
        return [("cpu", None)]
    needs: list[tuple[str, Optional[str]]] = []
    for m in metrics:
        if not isinstance(m, dict):
            continue
        if m.get("type") == "Resource":
            r = m.get("resource") or {}
            container = None
        elif m.get("type") == "ContainerResource":
            r = m.get("containerResource") or {}
            container = r.get("container")
        else:
            continue
        target = r.get("target") or {}
        utilization = target.get("type") == "Utilization" or "targetAverageUtilization" in r
        if utilization and r.get("name"):
            needs.append((str(r["name"]), container))
    return needs


def res003_hpa_without_requests(ctx: Ctx) -> list[Hit]:
    hits = []
    for hpa in ctx.inv.hpas:
        w = ctx.inv.hpa_targets.get(hpa.ref.key)
        if w is None or not w.containers:
            continue
        missing = []
        for res, only in _hpa_resource_needs(hpa):
            for c in w.containers:
                if only and c.get("name") != only:
                    continue
                requests = ((c.get("resources") or {}).get("requests")) or {}
                if res not in requests:
                    missing.append(f"container '{c.get('name', '?')}' has no {res} request")
        if missing:
            hits.append(
                Hit(
                    "RES003",
                    "HPA targets resource utilization but " + "; ".join(dict.fromkeys(missing))
                    + " — the HPA can't compute utilization and never scales",
                    [w.obj, hpa],
                )
            )
    return hits


def res004_hpa_min_one(ctx: Ctx) -> list[Hit]:
    hits = []
    for hpa in ctx.inv.hpas:
        raw = hpa.spec.get("minReplicas")
        if raw is None or (_int_or_pct(raw) or ("int", 1.0))[1] <= 1:
            w = ctx.inv.hpa_targets.get(hpa.ref.key)
            objs = [w.obj, hpa] if w else [hpa]
            shown = "unset (defaults to 1)" if raw is None else str(raw)
            hits.append(Hit("RES004", f"minReplicas is {shown} — at low traffic this runs a single pod", objs))
    return hits


# ── spread ────────────────────────────────────────────────────────────────────

def res005_no_spread(ctx: Ctx) -> list[Hit]:
    if ctx.facts.nodes is not None and ctx.facts.nodes <= 1:
        return []  # one node: there is nowhere to spread to
    hits = []
    for w in ctx.inv.workloads:
        n = w.expected_replicas
        if not w.replicated or n is None or n <= 1:
            continue
        ps = w.pod_spec
        if (ps.get("affinity") or {}).get("podAntiAffinity") or ps.get("topologySpreadConstraints"):
            continue
        where = "nodes"
        if ctx.facts.zones and len(ctx.facts.zones) > 1:
            where = f"nodes or the cluster's {len(ctx.facts.zones)} zones (topology.kubernetes.io/zone)"
        hits.append(
            Hit(
                "RES005",
                f"{n} replicas with no podAntiAffinity or topologySpreadConstraints — only the scheduler's "
                f"soft default spreading keeps them off a single node; nothing requires spreading across {where}",
                [w.obj],
            )
        )
    return hits


# ── probes ────────────────────────────────────────────────────────────────────

def _serving_containers(w: Workload) -> list[dict]:
    with_ports = [c for c in w.containers if c.get("ports")]
    return with_ports or w.containers


def res006_no_readiness_probe(ctx: Ctx) -> list[Hit]:
    hits = []
    for w in ctx.inv.workloads:
        if not w.services:
            continue
        names = [c.get("name", "?") for c in _serving_containers(w) if not c.get("readinessProbe")]
        if names:
            hits.append(
                Hit(
                    "RES006",
                    "Service sends traffic as soon as the container starts — no readinessProbe on "
                    + ", ".join(f"'{n}'" for n in names),
                    [w.obj],
                )
            )
    return hits


def res007_liveness_equals_readiness(ctx: Ctx) -> list[Hit]:
    hits = []
    for w in ctx.inv.workloads:
        names = [
            c.get("name", "?")
            for c in w.containers
            if c.get("livenessProbe") and c.get("livenessProbe") == c.get("readinessProbe")
        ]
        if names:
            hits.append(
                Hit(
                    "RES007",
                    "livenessProbe is identical to readinessProbe on "
                    + ", ".join(f"'{n}'" for n in names)
                    + " — a slow dependency restarts the pod instead of just taking it out of rotation",
                    [w.obj],
                )
            )
    return hits


# ── shutdown ──────────────────────────────────────────────────────────────────

_SLEEP = re.compile(r"\bsleep\s+(\d+)")


def _prestop_sleep(c: dict) -> Optional[int]:
    pre = ((c.get("lifecycle") or {}).get("preStop")) or {}
    sleep = pre.get("sleep") or {}
    if isinstance(sleep, dict) and isinstance(sleep.get("seconds"), int):
        return sleep["seconds"]
    cmd = (pre.get("exec") or {}).get("command")
    if isinstance(cmd, list):
        m = _SLEEP.search(" ".join(str(p) for p in cmd))
        if m:
            return int(m.group(1))
    return None


def res008_no_prestop(ctx: Ctx) -> list[Hit]:
    hits = []
    for w in ctx.inv.workloads:
        if not w.services:
            continue
        if not any(((c.get("lifecycle") or {}).get("preStop")) for c in w.containers):
            hits.append(
                Hit(
                    "RES008",
                    "no preStop hook — pods can be killed before the Service stops routing to them, "
                    "dropping in-flight requests on every rollout",
                    [w.obj],
                )
            )
    return hits


def res009_grace_shorter_than_prestop(ctx: Ctx) -> list[Hit]:
    hits = []
    for w in ctx.inv.workloads:
        grace = w.pod_spec.get("terminationGracePeriodSeconds", 30)
        if not isinstance(grace, int):
            continue
        for c in w.containers:
            sleep = _prestop_sleep(c)
            if sleep is not None and grace <= sleep:
                hits.append(
                    Hit(
                        "RES009",
                        f"terminationGracePeriodSeconds={grace} but '{c.get('name', '?')}' sleeps {sleep}s in preStop "
                        "— the pod is SIGKILLed before the app ever gets SIGTERM",
                        [w.obj],
                    )
                )
    return hits


# ── hygiene ───────────────────────────────────────────────────────────────────

def res010_resources(ctx: Ctx) -> list[Hit]:
    hits = []
    for w in ctx.inv.workloads:
        parts = []
        for c in w.containers:
            res = c.get("resources") or {}
            req = res.get("requests") or {}
            lim = res.get("limits") or {}
            gaps = [g for g, ok in (("cpu request", "cpu" in req), ("memory request", "memory" in req), ("memory limit", "memory" in lim)) if not ok]
            if gaps:
                parts.append(f"'{c.get('name', '?')}': no " + ", no ".join(gaps))
        if parts:
            hits.append(Hit("RES010", "; ".join(parts), [w.obj]))
    return hits


def _bad_image(image: str) -> bool:
    if "@" in image:
        return False  # pinned by digest
    last = image.rsplit("/", 1)[-1]
    if ":" not in last:
        return True
    return last.rsplit(":", 1)[1] == "latest"


def res011_image_tag(ctx: Ctx) -> list[Hit]:
    hits = []
    for w in ctx.inv.workloads:
        bad = [str(c.get("image")) for c in w.containers if isinstance(c.get("image"), str) and _bad_image(c["image"])]
        if bad:
            hits.append(Hit("RES011", "unpinned image " + ", ".join(f"'{i}'" for i in bad) + " — a restart can pull different code", [w.obj]))
    return hits


# ── rollout ───────────────────────────────────────────────────────────────────

def res012_rollout_takes_all(ctx: Ctx) -> list[Hit]:
    hits = []
    for w in ctx.inv.workloads:
        n = w.expected_replicas
        if not w.replicated or not n or n < 1:
            continue
        spec = w.obj.spec
        if w.kind == "Deployment":
            strategy = spec.get("strategy") or {}
            if strategy.get("type", "RollingUpdate") != "RollingUpdate":
                continue
            raw = (strategy.get("rollingUpdate") or {}).get("maxUnavailable", "25%")
        else:  # StatefulSet: only when set explicitly
            raw = ((spec.get("updateStrategy") or {}).get("rollingUpdate") or {}).get("maxUnavailable")
            if raw is None:
                continue
        v = _int_or_pct(raw)
        if v is None:
            continue
        down = v[1] if v[0] == "int" else math.floor(n * v[1] / 100)
        if down >= n:
            hits.append(Hit("RES012", f"maxUnavailable={raw} with {_who([w])} — a rollout can stop every pod at once", [w.obj]))
    return hits


# ── wiring ────────────────────────────────────────────────────────────────────

def res013_service_orphan(ctx: Ctx) -> list[Hit]:
    hits = []
    for svc in ctx.inv.services:
        if svc.spec.get("type") == "ExternalName" or not svc.spec.get("selector"):
            continue
        if not ctx.inv.service_targets.get(svc.ref.key):
            hits.append(
                Hit(
                    "RES013",
                    "selector " + ", ".join(f"{k}={v}" for k, v in svc.spec["selector"].items())
                    + " matches no workload" + (" in the scanned manifests" if ctx.static else ""),
                    [svc],
                    advisory=ctx.static,
                )
            )
    return hits


def res014_pdb_orphan(ctx: Ctx) -> list[Hit]:
    hits = []
    for pdb in ctx.inv.pdbs:
        if not ctx.inv.pdb_targets.get(pdb.ref.key):
            hits.append(
                Hit(
                    "RES014",
                    "selector matches no workload" + (" in the scanned manifests" if ctx.static else "") + " — it protects nothing",
                    [pdb],
                    advisory=ctx.static,
                )
            )
    return hits


RULES: dict[str, Callable[[Ctx], list[Hit]]] = {
    "RES001": res001_pdb_blocks_eviction,
    "RES002": res002_single_replica_no_pdb,
    "RES003": res003_hpa_without_requests,
    "RES004": res004_hpa_min_one,
    "RES005": res005_no_spread,
    "RES006": res006_no_readiness_probe,
    "RES007": res007_liveness_equals_readiness,
    "RES008": res008_no_prestop,
    "RES009": res009_grace_shorter_than_prestop,
    "RES010": res010_resources,
    "RES011": res011_image_tag,
    "RES012": res012_rollout_takes_all,
    "RES013": res013_service_orphan,
    "RES014": res014_pdb_orphan,
}
