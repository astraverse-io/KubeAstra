"""Fetch and index a repo's manifests. No git, no disk — tarball in memory."""
from __future__ import annotations

import io
import tarfile
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

import yaml


@dataclass(frozen=True)
class RepoFile:
    path: str
    text: str


@dataclass(frozen=True)
class ResourceMatch:
    file_path: str
    doc_index: int
    namespace: str | None


_YAML_SUFFIXES = (".yaml", ".yml")


def read_tarball(blob: bytes) -> list[RepoFile]:
    out: list[RepoFile] = []
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile() or not member.name.endswith(_YAML_SUFFIXES):
                continue
            # GitHub prefixes every path with "owner-repo-sha/". Strip it.
            rel = member.name.split("/", 1)[1] if "/" in member.name else member.name
            fh = tf.extractfile(member)
            if fh is None:
                continue
            out.append(RepoFile(rel, fh.read().decode("utf-8", "replace")))
    return out


def build_index(files: Iterable[RepoFile]) -> dict[tuple[str, str], list[ResourceMatch]]:
    index: dict[tuple[str, str], list[ResourceMatch]] = defaultdict(list)
    for f in files:
        try:
            docs = list(yaml.safe_load_all(f.text))
        except yaml.YAMLError:
            continue  # templated / non-YAML — skip, never fatal
        for i, doc in enumerate(docs):
            if not isinstance(doc, dict):
                continue
            kind = doc.get("kind")
            meta = doc.get("metadata") or {}
            name = meta.get("name") if isinstance(meta, dict) else None
            if not kind or not name:
                continue
            ns = meta.get("namespace") if isinstance(meta, dict) else None
            index[(kind, name)].append(ResourceMatch(f.path, i, ns))
    return dict(index)


def detect_markers(files: Iterable[RepoFile]) -> set[str]:
    markers: set[str] = set()
    for f in files:
        base = f.path.rsplit("/", 1)[-1]
        if base == "Chart.yaml":
            markers.add("helm")
        if "HelmRelease" in f.text:
            markers.add("helm")
        if "kind: Application" in f.text and "argoproj.io" in f.text:
            markers.add("argo")
    return markers
