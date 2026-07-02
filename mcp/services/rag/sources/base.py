"""Source protocol + Document dataclass.

Sources are intentionally minimal: discover documents and let the
ingestion pipeline do everything else (chunking, embedding, upsert).
This keeps it trivial to add new source kinds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol, runtime_checkable


@dataclass
class Document:
    """A single ingestable document, before chunking."""
    url: str                # canonical reference (file:// or https://)
    title: str              # display name for citations
    content: str            # raw markdown / text
    # Optional metadata propagated to every chunk's payload.
    metadata: dict = field(default_factory=dict)


@runtime_checkable
class Source(Protocol):
    """Anything that yields Documents.

    Implementations should be lazy where possible — ``discover()`` is an
    iterator so the pipeline can stream large corpora without loading
    everything into memory.
    """
    name: str

    def discover(self) -> Iterable[Document]: ...
