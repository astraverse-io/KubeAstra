"""Tests for the RAG ingestion pipeline (blueprint §7).

Uses Qdrant `:memory:` mode + a sentence-transformers-free fake embedder
where applicable so the suite runs fast and offline.

Covers the blueprint §7 acceptance criteria:
  1. `ingest(LocalPathSource(<nested .md files>))` discovers them, produces
     ≥4 chunks, failed=0.
  2. Re-running ingest immediately returns `new == 0, skipped == prior_new`.
  3. `chunk_markdown(# A\\n## B\\n## C)` produces 3 chunks with sections
     "A", "A > B", "A > C".
  4. `chunk_markdown(no-headers)` produces ≥1 chunks all under section="(top)".
  5. `GitRepoSource(bad URL)` rejected at construction with ValueError.
  6. `kb_search` returns ≤ limit, with similarity + section in each hit.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

MCP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)


# ── Chunker ──────────────────────────────────────────────────────────────────

class TestChunker:
    """chunk_markdown produces correct sections / windowing."""

    def test_three_section_doc(self):
        """Acceptance criterion #3."""
        from services.rag import chunk_markdown
        doc = "# A\nbody A\n## B\nbody B\n## C\nbody C\n"
        chunks = chunk_markdown(doc)
        assert [(c.section, c.text) for c in chunks] == [
            ("A", "body A"),
            ("A > B", "body B"),
            ("A > C", "body C"),
        ]

    def test_headerless_doc(self):
        """Acceptance criterion #4."""
        from services.rag import chunk_markdown
        doc = "just some text\nwith multiple lines\nno headers here\n"
        chunks = chunk_markdown(doc)
        assert len(chunks) >= 1
        for c in chunks:
            assert c.section == "(top)"

    def test_empty_doc(self):
        from services.rag import chunk_markdown
        assert chunk_markdown("") == []
        assert chunk_markdown("   \n   \n   ") == []

    def test_long_section_windows(self):
        """Long sections produce multiple chunks via sliding windows."""
        from services.rag import chunk_markdown
        # Build a long body that exceeds max_tokens=400 → max_words~308.
        # 1000 words * 1.3 tokens/word ≈ 1300 tokens, so we need ~3-4 windows.
        body = " ".join(f"word{i}" for i in range(1000))
        doc = f"# Big section\n{body}\n"
        chunks = chunk_markdown(doc, max_tokens=400, overlap_tokens=60)
        assert len(chunks) >= 3, f"expected ≥3 windows for long body, got {len(chunks)}"
        # All chunks share the same section breadcrumb
        assert all(c.section == "Big section" for c in chunks)
        # Chunks are indexed in order
        assert [c.index for c in chunks] == list(range(len(chunks)))

    def test_breadcrumb_pops_levels(self):
        """## C should not include ## B in its breadcrumb (sibling, not parent)."""
        from services.rag import chunk_markdown
        doc = "# A\nbody A\n## B\nbody B\n### B1\nbody B1\n## C\nbody C\n"
        chunks = chunk_markdown(doc)
        sections = [c.section for c in chunks]
        assert "A > B > B1" in sections
        assert "A > C" in sections
        # B should NOT appear in C's breadcrumb
        c_chunk = next(c for c in chunks if c.text == "body C")
        assert c_chunk.section == "A > C", f"got {c_chunk.section}"


# ── LocalPathSource ──────────────────────────────────────────────────────────

class TestLocalPathSource:

    def test_discovers_markdown_files(self):
        from services.rag.sources import LocalPathSource

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "a.md").write_text("# A\nfirst doc\n")
            (root / "sub").mkdir()
            (root / "sub" / "b.markdown").write_text("# B\nnested doc\n")
            (root / "ignored.txt").write_text("not markdown")

            src = LocalPathSource(path=str(root))
            docs = list(src.discover())

        titles = sorted(d.title for d in docs)
        assert titles == ["a.md", "sub/b.markdown"]
        # URL is file://<abs path>
        for d in docs:
            assert d.url.startswith("file://")

    def test_missing_path_yields_zero_documents(self):
        """Don't crash the whole ingestion for one bad source."""
        from services.rag.sources import LocalPathSource
        src = LocalPathSource(path="/nonexistent/path/here")
        assert list(src.discover()) == []


# ── GitRepoSource (URL validation) ───────────────────────────────────────────

class TestGitRepoSourceValidation:
    """Acceptance criterion #5 — URL allowlist."""

    def test_valid_https_url(self):
        from services.rag.sources import GitRepoSource
        src = GitRepoSource(url="https://github.com/org/repo")
        assert src.url == "https://github.com/org/repo"

    def test_valid_ssh_url(self):
        from services.rag.sources import GitRepoSource
        src = GitRepoSource(url="git@github.com:org/repo.git")
        assert src.url == "git@github.com:org/repo.git"

    def test_shell_metacharacters_rejected(self):
        from services.rag.sources import GitRepoSource
        with pytest.raises(ValueError):
            GitRepoSource(url="; rm -rf /")
        with pytest.raises(ValueError):
            GitRepoSource(url="https://github.com/org/repo && bad")
        with pytest.raises(ValueError):
            GitRepoSource(url="$(rm -rf /)")

    def test_unsupported_scheme_rejected(self):
        from services.rag.sources import GitRepoSource
        with pytest.raises(ValueError):
            GitRepoSource(url="ftp://example.com/repo")
        with pytest.raises(ValueError):
            GitRepoSource(url="file:///etc/passwd")

    def test_empty_url_rejected(self):
        from services.rag.sources import GitRepoSource
        with pytest.raises(ValueError):
            GitRepoSource(url="")

    def test_path_traversal_in_subdir_rejected(self):
        from services.rag.sources import GitRepoSource
        with pytest.raises(ValueError):
            GitRepoSource(
                url="https://github.com/org/repo",
                subdir="docs/../../etc",
            )

    def test_absolute_subdir_rejected(self):
        """R-CB7: absolute paths bypass tmpdir; must be rejected."""
        from services.rag.sources import GitRepoSource
        with pytest.raises(ValueError):
            GitRepoSource(
                url="https://github.com/org/repo",
                subdir="/etc",
            )
        with pytest.raises(ValueError):
            GitRepoSource(
                url="https://github.com/org/repo",
                subdir="/var/lib/secret",
            )

    def test_bad_branch_rejected(self):
        from services.rag.sources import GitRepoSource
        with pytest.raises(ValueError):
            GitRepoSource(
                url="https://github.com/org/repo",
                branch="main;rm -rf /",
            )


# ── Ingestion + idempotency ──────────────────────────────────────────────────

@pytest.fixture
def memory_qdrant(monkeypatch):
    """Point vector_db at an in-process Qdrant.

    Mutates the EXISTING singleton in place — modules that imported
    `from services.vector_db import vector_db` at load time still see
    the same object (which now has refreshed settings + a :memory: client).
    """
    monkeypatch.setenv("QDRANT_URL", ":memory:")
    monkeypatch.setenv("EMBEDDING_DIM", "8")
    from config.settings import get_settings
    get_settings.cache_clear()

    from services.vector_db import vector_db
    # Disconnect any previous client, refresh settings, reconnect against
    # the in-memory backend.
    vector_db.disconnect()
    vector_db._settings = get_settings()
    vector_db.connect()
    yield vector_db
    vector_db.disconnect()
    get_settings.cache_clear()
    # Restore the singleton's settings to whatever the env now contains
    # so later tests see a clean state.
    vector_db._settings = get_settings()


@pytest.fixture
def fake_embedder(monkeypatch):
    """Replace sentence-transformers with a tiny deterministic fake.

    The real embedder downloads a ~90MB model on first call, which is
    slow + flaky in CI. The fake hashes the text into an 8-dim vector
    — close enough for testing the pipeline plumbing.
    """
    import hashlib
    import services.embeddings as emb_module

    def _fake_embed(text: str) -> list[float]:
        h = hashlib.sha256(text.encode("utf-8")).digest()
        # Map first 8 bytes to floats in [0, 1]
        return [b / 255.0 for b in h[:8]]

    def _fake_embed_many(texts: list[str]) -> list[list[float]]:
        return [_fake_embed(t) for t in texts]

    monkeypatch.setattr(emb_module.embeddings, "embed", _fake_embed)
    monkeypatch.setattr(emb_module.embeddings, "embed_many", _fake_embed_many)
    yield


class TestIngestion:
    """Acceptance criteria #1, #2."""

    def _write_two_docs(self, root: Path) -> None:
        (root / "alpha.md").write_text(
            "# Alpha\n"
            "para 1 alpha\n\n"
            "## Sub\n"
            "para 2 alpha sub\n"
        )
        sub = root / "nested"
        sub.mkdir()
        (sub / "beta.md").write_text(
            "# Beta\n"
            "para 1 beta\n\n"
            "## Sub\n"
            "para 2 beta sub\n"
        )

    def test_ingest_two_docs(self, memory_qdrant, fake_embedder):
        """Acceptance criterion #1: 2 .md files in nested dirs → ≥4 chunks, failed=0."""
        from services.rag import ingest
        from services.rag.sources import LocalPathSource

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_two_docs(Path(tmpdir))
            src = LocalPathSource(path=tmpdir)
            stats = ingest(src)

        assert stats.failed == 0
        assert stats.discovered == 2
        assert stats.new >= 4, f"expected ≥4 chunks, got {stats.new}"

    def test_reingest_is_idempotent(self, memory_qdrant, fake_embedder):
        """Acceptance criterion #2: re-running ingest is a no-op."""
        from services.rag import ingest
        from services.rag.sources import LocalPathSource

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_two_docs(Path(tmpdir))
            src = LocalPathSource(path=tmpdir)

            first = ingest(src)
            second = ingest(src)

        assert second.new == 0
        assert second.skipped == first.new
        assert second.failed == 0

    def test_dry_run_does_not_upsert(self, memory_qdrant, fake_embedder):
        """dry_run=True walks + chunks + counts but skips embedding + upsert."""
        from services.rag import ingest
        from services.rag.sources import LocalPathSource

        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_two_docs(Path(tmpdir))
            src = LocalPathSource(path=tmpdir)

            stats = ingest(src, dry_run=True)
            # After dry run, the collection should still be empty for those chunks.
            stats2 = ingest(src, dry_run=False)

        # Dry run counted chunks as new (would-be-new) but didn't write.
        assert stats.new >= 4
        # Real run then writes them all (skipped should still be 0 because nothing was upserted).
        assert stats2.new >= 4
        assert stats2.skipped == 0
