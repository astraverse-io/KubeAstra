"""ClusterSnapshot — the plain-data input to the Upgrade Pilot core.

A caller (the server's scan tools, the ``kubeastra upgrade`` CLI, or the GitHub
Action in static mode) builds one of these however it can, then hands it to
:func:`assess`/:func:`plan`. Pure data: dataclasses + JSON (de)serialization,
stdlib only. Nothing here talks to a cluster — building the snapshot is the
caller's job (Phase 1); this module just defines its shape.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ObjectRef:
    """An object in use on the cluster (or in rendered manifests)."""

    gvk: str  # e.g. "apps/v1/Deployment", "batch/v1beta1/CronJob"
    kind: str
    name: str
    namespace: str = ""
    source: str = "manifest"  # "manifest" | "helm" | "operator"
    owner: dict[str, str] = field(default_factory=dict)  # ownerReference, if any


@dataclass
class HelmRelease:
    name: str
    namespace: str = ""
    chart: str = ""
    chart_version: str = ""
    app_version: str = ""


@dataclass
class OperatorInfo:
    name: str
    version: str = ""
    crds: list[str] = field(default_factory=list)


@dataclass
class ClusterSnapshot:
    """Everything the pure planner needs, with no live-cluster dependency."""

    cluster_version: str = ""
    node_kubelet_versions: list[str] = field(default_factory=list)
    provider: str = "unknown"  # "eks" | "gke" | "aks" | "unknown"
    objects: list[ObjectRef] = field(default_factory=list)
    helm_releases: list[HelmRelease] = field(default_factory=list)
    operators: list[OperatorInfo] = field(default_factory=list)
    source_mode: str = "live"  # "live" | "static"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ClusterSnapshot":
        """Rebuild from a plain dict (e.g. JSON off the wire). Unknown keys are
        ignored so a newer producer never breaks an older consumer."""
        data = dict(data or {})
        return cls(
            cluster_version=data.get("cluster_version", ""),
            node_kubelet_versions=list(data.get("node_kubelet_versions") or []),
            provider=data.get("provider", "unknown"),
            objects=[ObjectRef(**o) for o in (data.get("objects") or [])],
            helm_releases=[HelmRelease(**h) for h in (data.get("helm_releases") or [])],
            operators=[OperatorInfo(**op) for op in (data.get("operators") or [])],
            source_mode=data.get("source_mode", "live"),
        )
