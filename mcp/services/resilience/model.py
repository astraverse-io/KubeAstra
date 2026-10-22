"""Turn manifest documents into a linked inventory the rules can reason over.

Each workload is linked to the HPA that scales it (``scaleTargetRef``), the PDBs
whose selector covers its pod template, and the Services whose selector does.
Links are namespace-scoped; in static mode a document with no namespace is
treated as "whatever namespace it gets applied to", so it links with anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .selectors import label_selector_matches, map_selector_matches
from .types import Ref

REPLICATED = ("Deployment", "StatefulSet")
WORKLOAD_KINDS = REPLICATED + ("DaemonSet", "Job", "CronJob")

IGNORE_ANNOTATION = "resilience.kubeastra.io/ignore"


@dataclass
class Obj:
    kind: str
    name: str
    namespace: str
    doc: dict
    ref: Ref

    @property
    def spec(self) -> dict:
        s = self.doc.get("spec")
        return s if isinstance(s, dict) else {}

    @property
    def annotations(self) -> dict:
        a = (self.doc.get("metadata") or {}).get("annotations")
        return a if isinstance(a, dict) else {}

    @property
    def labels(self) -> dict:
        lab = (self.doc.get("metadata") or {}).get("labels")
        return lab if isinstance(lab, dict) else {}

    @property
    def helm_owned(self) -> bool:
        """Rendered by Helm — the same test the Upgrade Pilot's scan uses."""
        return self.labels.get("app.kubernetes.io/managed-by") == "Helm" or "helm.sh/chart" in self.labels

    def ignores(self, rule: str) -> bool:
        raw = str(self.annotations.get(IGNORE_ANNOTATION, ""))
        ids = {p.strip().upper() for p in raw.split(",") if p.strip()}
        return "*" in ids or rule.upper() in ids


@dataclass
class Workload:
    obj: Obj
    hpa: Optional[Obj] = None
    pdbs: list[Obj] = field(default_factory=list)
    services: list[Obj] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return self.obj.kind

    @property
    def replicated(self) -> bool:
        return self.kind in REPLICATED

    @property
    def template(self) -> dict:
        spec = self.obj.spec
        if self.kind == "CronJob":
            spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
        t = spec.get("template")
        return t if isinstance(t, dict) else {}

    @property
    def pod_labels(self) -> dict:
        lab = (self.template.get("metadata") or {}).get("labels")
        return lab if isinstance(lab, dict) else {}

    @property
    def pod_spec(self) -> dict:
        s = self.template.get("spec")
        return s if isinstance(s, dict) else {}

    @property
    def containers(self) -> list[dict]:
        return [c for c in (self.pod_spec.get("containers") or []) if isinstance(c, dict)]

    @property
    def replicas(self) -> Optional[int]:
        """``spec.replicas`` (API default 1) for Deployments/StatefulSets, else ``None``."""
        if not self.replicated:
            return None
        return _as_int(self.obj.spec.get("replicas"), default=1)

    @property
    def expected_replicas(self) -> Optional[int]:
        """The replica count to plan disruptions against: the HPA floor when an
        HPA owns scaling (the worst case — a drain at low traffic), else ``replicas``."""
        if not self.replicated:
            return None
        if self.hpa is not None:
            return _as_int(self.hpa.spec.get("minReplicas"), default=1)
        return self.replicas


@dataclass
class Inventory:
    workloads: list[Workload] = field(default_factory=list)
    hpas: list[Obj] = field(default_factory=list)
    pdbs: list[Obj] = field(default_factory=list)
    services: list[Obj] = field(default_factory=list)
    pdb_targets: dict[tuple, list[Workload]] = field(default_factory=dict)
    service_targets: dict[tuple, list[Workload]] = field(default_factory=dict)
    hpa_targets: dict[tuple, Optional[Workload]] = field(default_factory=dict)


def _as_int(v: Any, default: int) -> int:
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return default


def same_namespace(a: str, b: str) -> bool:
    return a == b or not a or not b


def _expand(doc: Any) -> Iterable[dict]:
    """A document, or the items of a ``kind: List`` (``kubectl get -o yaml`` output)."""
    if not isinstance(doc, dict):
        return []
    if doc.get("kind") == "List" and isinstance(doc.get("items"), list):
        return [d for d in doc["items"] if isinstance(d, dict)]
    return [doc]


def _to_obj(doc: dict, file: str, line: int) -> Optional[Obj]:
    kind = doc.get("kind")
    if not doc.get("apiVersion") or not isinstance(kind, str):
        return None
    meta = doc.get("metadata") or {}
    if not isinstance(meta, dict):
        return None
    name = str(meta.get("name") or "")
    ns = str(meta.get("namespace") or "")
    return Obj(kind=kind, name=name, namespace=ns, doc=doc, ref=Ref(kind=kind, name=name, namespace=ns, file=file, line=line))


def objects_from_docs(items: Iterable[Any]) -> list[Obj]:
    """Accepts ``ManifestDoc``s (from :func:`services.manifests.iter_manifest_docs`)
    or plain dicts (live mode / tests)."""
    out: list[Obj] = []
    for item in items:
        if hasattr(item, "doc") and hasattr(item, "path"):
            doc, file, line = item.doc, str(item.path), int(getattr(item, "line", 0))
        else:
            doc, file, line = item, "", 0
        for d in _expand(doc):
            o = _to_obj(d, file, line if d is doc else 0)
            if o is not None:
                out.append(o)
    return out


def build_inventory(objs: Iterable[Obj]) -> Inventory:
    inv = Inventory()
    for o in objs:
        if o.kind in WORKLOAD_KINDS:
            inv.workloads.append(Workload(obj=o))
        elif o.kind == "HorizontalPodAutoscaler":
            inv.hpas.append(o)
        elif o.kind == "PodDisruptionBudget":
            inv.pdbs.append(o)
        elif o.kind == "Service":
            inv.services.append(o)

    for hpa in inv.hpas:
        target = hpa.spec.get("scaleTargetRef") or {}
        match = None
        for w in inv.workloads:
            if (
                w.kind == target.get("kind")
                and w.obj.name == target.get("name")
                and same_namespace(w.obj.namespace, hpa.namespace)
            ):
                match = w
                break
        inv.hpa_targets[hpa.ref.key] = match
        if match is not None and match.hpa is None:
            match.hpa = hpa

    for pdb in inv.pdbs:
        hits = [
            w
            for w in inv.workloads
            if w.kind not in ("Job", "CronJob")
            and same_namespace(w.obj.namespace, pdb.namespace)
            and label_selector_matches(pdb.spec.get("selector"), w.pod_labels)
        ]
        inv.pdb_targets[pdb.ref.key] = hits
        for w in hits:
            w.pdbs.append(pdb)

    for svc in inv.services:
        hits = [
            w
            for w in inv.workloads
            if w.kind not in ("Job", "CronJob")
            and same_namespace(w.obj.namespace, svc.namespace)
            and map_selector_matches(svc.spec.get("selector"), w.pod_labels)
        ]
        inv.service_targets[svc.ref.key] = hits
        for w in hits:
            w.services.append(svc)
    return inv
