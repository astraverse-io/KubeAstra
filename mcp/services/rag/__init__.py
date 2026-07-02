"""RAG (retrieval-augmented generation) package — Phase 1.2.

Public surface (kept tight to avoid coupling):
    schema     — Collection definitions
    chunking   — Markdown-aware splitter
    sources    — Pluggable document sources (local_path, git_repo)
    ingestion  — Orchestrator: discover → chunk → embed → upsert
"""
