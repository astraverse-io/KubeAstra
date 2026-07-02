"""Pluggable document sources for the RAG ingestion pipeline.

Each source implements the ``Source`` protocol: a ``name`` attribute
plus a ``discover()`` method that yields ``Document`` instances.

Phase 1.2 ships two source implementations:
- ``local_path``: walks a directory tree for ``*.md`` files
- ``git_repo``: shallow-clones a git repo and delegates to local_path

Future phases can add ``confluence``, ``notion``, etc. by following
the same protocol without touching the orchestrator.
"""

from .base import Document, Source

__all__ = ["Document", "Source"]
