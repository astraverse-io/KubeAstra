"""Regression tests for the post-ship review fixes.

These pin down the behaviors we just added so the review-pass changes
don't quietly regress.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from services.rag.chunking_ansible import _DOC_BLOCK_RE, chunk_ansible_module
from services.rag.sources.git_repo import _scrub_token
from services.rag.sources.local_path import _derive_ansible_metadata


# ── Bug 1: token scrub ──────────────────────────────────────────────────────

def test_scrub_token_replaces_value(monkeypatch):
    monkeypatch.setenv("FAKE_TOKEN", "ghp_secretvalue123")
    msg = "fatal: could not read from https://oauth2:ghp_secretvalue123@github.com/x/y"
    assert "ghp_secretvalue123" not in _scrub_token(msg, "FAKE_TOKEN")
    assert "<redacted>" in _scrub_token(msg, "FAKE_TOKEN")


def test_scrub_token_noop_when_no_env_var():
    msg = "fatal: anything"
    assert _scrub_token(msg, None) == msg
    assert _scrub_token(msg, "DEFINITELY_NOT_SET_XYZ") == msg


def test_scrub_token_noop_when_env_empty(monkeypatch):
    monkeypatch.setenv("EMPTY_TOKEN", "")
    msg = "fatal: anything"
    assert _scrub_token(msg, "EMPTY_TOKEN") == msg


# ── Issue 4: role-local custom modules get module_name ─────────────────────

def test_role_local_library_module_gets_module_name():
    rel = Path("roles/kubeastra/sym_topology/library/queue_helper.py")
    meta = _derive_ansible_metadata(rel)
    assert meta["category"] == "kubeastra"
    assert meta["role"] == "sym_topology"
    assert meta["module_name"] == "queue_helper"


def test_role_library_at_depth_5_also_sets_role_subdir():
    """A library/ at depth 4-from-roles becomes role_subdir AND gets
    module_name from the .py stem."""
    rel = Path("roles/cat/role/library/mod.py")
    meta = _derive_ansible_metadata(rel)
    assert meta["role"] == "role"
    assert meta["role_subdir"] == "library"
    assert meta["module_name"] == "mod"


# ── Issue 5: batch existence check ─────────────────────────────────────────

def test_exists_many_returns_subset_of_present_ids():
    """Smoke against an in-memory Qdrant to confirm batch retrieve
    actually returns the intersection of asked-for and present ids."""
    from qdrant_client import QdrantClient
    from qdrant_client.http import models as qmodels

    from services import vector_db as vdb_module
    from services.rag.schema import DEPLOYMENT_REPO

    client = QdrantClient(":memory:")
    vdb_module.vector_db._client = client
    vdb_module.vector_db.ensure_collection_for(DEPLOYMENT_REPO)

    # Insert two known points
    known_ids = [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    ]
    client.upsert(
        collection_name=DEPLOYMENT_REPO.name,
        points=[
            qmodels.PointStruct(id=i, vector=[0.0] * 384, payload={"x": 1})
            for i in known_ids
        ],
    )
    # Ask for one known + one unknown
    asked = known_ids + ["00000000-0000-0000-0000-000000000099"]
    present = vdb_module.vector_db.exists_many(DEPLOYMENT_REPO.name, asked)
    assert present == set(known_ids)


def test_exists_many_empty_input_returns_empty_set():
    from qdrant_client import QdrantClient

    from services import vector_db as vdb_module
    from services.rag.schema import DEPLOYMENT_REPO

    vdb_module.vector_db._client = QdrantClient(":memory:")
    vdb_module.vector_db.ensure_collection_for(DEPLOYMENT_REPO)
    assert vdb_module.vector_db.exists_many(DEPLOYMENT_REPO.name, []) == set()


# ── Issue 6: DOCUMENTATION regex accepts type-annotated form ────────────────

def test_doc_block_matches_type_annotated():
    src = (
        "#!/usr/bin/python\n"
        "DOCUMENTATION: str = r'''\n"
        "module: ann_mod\n"
        "short_description: with type annotation\n"
        "'''\n\n"
        "def main(): pass\n"
    )
    chunks = chunk_ansible_module(src, {"module_name": "ann_mod"})
    assert chunks[0].extra["doc_extracted"] is True
    assert "ann_mod" in chunks[0].text


def test_doc_block_matches_parenthesized_form():
    src = (
        "DOCUMENTATION = (r'''\n"
        "module: paren_mod\n"
        "''')\n"
    )
    chunks = chunk_ansible_module(src, {"module_name": "paren_mod"})
    assert chunks[0].extra["doc_extracted"] is True
    assert "paren_mod" in chunks[0].text


def test_doc_block_still_matches_plain_form():
    """Don't regress the common case."""
    src = "DOCUMENTATION = '''\nmodule: plain_mod\n'''\n"
    chunks = chunk_ansible_module(src, {"module_name": "plain_mod"})
    assert chunks[0].extra["doc_extracted"] is True
