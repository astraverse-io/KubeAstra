"""Build a :class:`ClusterSnapshot` for the Upgrade Pilot.

Two keyless producers, both pure (``yaml`` + stdlib; no cluster access here —
the caller fetches, these only shape the data):

- :func:`scan_manifests` — STATIC mode: parse rendered manifests on disk
  (``helm template`` / ``kustomize build`` output, or raw YAML). Authoritative
  for the API-deprecation check; operator/helm-release discovery is limited
  (no live cluster), which the report notes.
- :func:`snapshot_from_cluster` — LIVE mode: assemble a snapshot from already
  fetched ``kubectl``/``helm`` JSON. The thin kubectl-calling tool lives in the
  server (``mcp/k8s``); this builder stays pure so the CLI/Action and tests use
  it without a cluster.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import yaml

from .snapshot import ClusterSnapshot, HelmRelease, ObjectRef, OperatorInfo

_MANIFEST_SUFFIXES = (".yaml", ".yml", ".json")


def split_api_version(api_version: str) -> tuple[str, str]:
    """"networking.k8s.io/v1" -> ("networking.k8s.io", "v1"); "v1" -> ("", "v1")."""
    if "/" in api_version:
        group, version = api_version.split("/", 1)
        return group, version
    return "", api_version


def _object_ref_from_doc(doc: Any) -> ObjectRef | None:
    if not isinstance(doc, dict):
        return None
    api_version = doc.get("apiVersion")
    kind = doc.get("kind")
    if not api_version or not kind:
        return None
    meta = doc.get("metadata") or {}
    owner: dict[str, str] = {}
    refs = meta.get("ownerReferences") or []
    if refs and isinstance(refs[0], dict):
        r0 = refs[0]
        owner = {
            "apiVersion": r0.get("apiVersion", ""),
            "kind": r0.get("kind", ""),
            "name": r0.get("name", ""),
        }
    labels = meta.get("labels") or {}
    source = "manifest"
    if owner:
        source = "operator"
    elif labels.get("app.kubernetes.io/managed-by") == "Helm" or "helm.sh/chart" in labels:
        source = "helm"
    return ObjectRef(
        gvk=f"{api_version}/{kind}",
        kind=kind,
        name=meta.get("name", ""),
        namespace=meta.get("namespace", ""),
        source=source,
        owner=owner,
    )


def scan_manifests(
    path: str | Path,
    *,
    cluster_version: str = "",
    target_hint: str = "",
) -> ClusterSnapshot:
    """Walk ``path`` for rendered manifests and build a static ClusterSnapshot.

    Each YAML document contributing an ``apiVersion``+``kind`` becomes an
    :class:`ObjectRef`. Unparseable documents are skipped (a bad file must not
    sink the whole scan). ``source_mode`` is "static".
    """
    root = Path(path)
    files: list[Path]
    if root.is_file():
        files = [root]
    else:
        files = sorted(p for p in root.rglob("*") if p.suffix.lower() in _MANIFEST_SUFFIXES)

    objects: list[ObjectRef] = []
    for f in files:
        try:
            docs = list(yaml.safe_load_all(f.read_text()))
        except Exception:
            continue  # a malformed file is skipped, not fatal
        for doc in docs:
            ref = _object_ref_from_doc(doc)
            if ref is not None:
                objects.append(ref)

    return ClusterSnapshot(
        cluster_version=cluster_version,
        objects=objects,
        source_mode="static",
    )


def snapshot_from_cluster(
    *,
    cluster_version: str = "",
    node_kubelet_versions: Iterable[str] = (),
    provider: str = "unknown",
    object_docs: Iterable[Any] = (),
    helm_releases: Iterable[dict] = (),
    operators: Iterable[dict] = (),
) -> ClusterSnapshot:
    """Assemble a LIVE snapshot from already-fetched data (kubectl/helm JSON).

    Pure: the caller does the I/O. ``object_docs`` are manifest-shaped dicts
    (``kubectl get ... -o json`` items); ``helm_releases`` are ``helm list -o
    json`` rows; ``operators`` are ``{name, version, crds}`` dicts.
    """
    objects = [r for r in (_object_ref_from_doc(d) for d in object_docs) if r is not None]
    helm = [
        HelmRelease(
            name=h.get("name", ""),
            namespace=h.get("namespace", ""),
            chart=h.get("chart", ""),
            chart_version=str(h.get("chart_version", h.get("chartVersion", ""))),
            app_version=str(h.get("app_version", h.get("appVersion", ""))),
        )
        for h in helm_releases
        if isinstance(h, dict)
    ]
    ops = [
        OperatorInfo(
            name=o.get("name", ""),
            version=str(o.get("version", "")),
            crds=list(o.get("crds", []) or []),
        )
        for o in operators
        if isinstance(o, dict)
    ]
    return ClusterSnapshot(
        cluster_version=cluster_version,
        node_kubelet_versions=list(node_kubelet_versions),
        provider=provider or "unknown",
        objects=objects,
        helm_releases=helm,
        operators=ops,
        source_mode="live",
    )
