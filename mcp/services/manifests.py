"""Read rendered Kubernetes manifests from disk — shared by the pure Pilot cores.

``helm template`` / ``kustomize build`` output or raw YAML/JSON. Pure: PyYAML +
stdlib, no cluster, no ``ui/``. Both the Upgrade Pilot (``services.upgrade``)
and the Resilience Pilot (``services.resilience``) walk manifests through here,
so they agree on what counts as a manifest file and on how bad files are handled.

Vendored into the CLI wheel by ``cli/scripts/vendor_core.py`` — keep it
dependency-free, and import it relatively (``from ..manifests import …``) so the
vendored copy resolves too.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import yaml

MANIFEST_SUFFIXES = (".yaml", ".yml", ".json")


@dataclass(frozen=True)
class ManifestDoc:
    """One YAML document from one file. ``line`` is 1-based (where the doc starts)."""

    path: Path
    index: int
    line: int
    doc: Any


def manifest_files(path: str | Path) -> list[Path]:
    """``path`` itself if it is a file, else every manifest file under it, sorted."""
    root = Path(path)
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in MANIFEST_SUFFIXES)


def _load_file(f: Path) -> list[ManifestDoc]:
    """Every document in ``f``, or ``[]`` if any of it fails to parse.

    All-or-nothing per file (a bad file must not sink the whole scan, and a
    half-parsed file must not yield a misleading subset). Line numbers come from
    the composed node, which ``safe_load_all`` doesn't expose.
    """
    out: list[ManifestDoc] = []
    try:
        loader = yaml.SafeLoader(f.read_text())
    except Exception:  # unreadable / undecodable file: skipped, like a malformed one
        return []
    try:
        index = 0
        while loader.check_node():
            node = loader.get_node()
            doc = loader.construct_document(node)
            out.append(ManifestDoc(path=f, index=index, line=node.start_mark.line + 1, doc=doc))
            index += 1
    except Exception:
        return []
    finally:
        loader.dispose()
    return out


def iter_manifest_docs(path: str | Path) -> Iterator[ManifestDoc]:
    """Yield every document under ``path``. Empty documents (``---`` alone) are
    yielded with ``doc=None``; callers decide what a usable document is."""
    for f in manifest_files(path):
        yield from _load_file(f)
