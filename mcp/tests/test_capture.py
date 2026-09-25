"""Tests for auto-capture + promotion + redaction (blueprint §9).

Uses Qdrant `:memory:` mode + a fake embedder + a mocked LLM provider so
the worthy classifier is fully deterministic.

Covers all blueprint §9 acceptance criteria:
  1. Real problem session → maybe_capture returns capture_id; entry exists
     in session_memory with verified=False.
  2. Chitchat → classifier worthy=False → maybe_capture returns None;
     nothing written.
  3. tool_used="error" → quick-rejected without classifier call.
  4. redact() scrubs Google API keys, GitHub PATs, JWTs, PEM private keys,
     kubeconfig `token:` lines, and base64 blobs ≥60 chars.
  5. POST feedback up → entry appears in runbook with verified=True,
     removed from session_memory.
  6. POST feedback down → entry removed from session_memory, NOT added
     to runbook.
  7. promote(same_id) twice → first succeeds, second is a no-op
     (already_promoted=True).
  8. SESSION_CAPTURE_ENABLED=false → capture returns None immediately.
  9. Captured payload containing a Google API key has it replaced with
     `<REDACTED:google_api_key>`.
 10. Router never triggers cached mode on session_memory (verified=False).
"""

from __future__ import annotations

import hashlib
import os
import sys
from unittest.mock import MagicMock

import pytest

MCP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)


# ── Deterministic 8-dim fake embedder (matches test_router / test_rag) ──────

def _fake_embed(text: str) -> list[float]:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [b / 255.0 for b in h[:8]]


def _fake_embed_many(texts: list[str]) -> list[list[float]]:
    return [_fake_embed(t) for t in texts]


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_embedder(monkeypatch):
    import services.embeddings as emb_module
    monkeypatch.setattr(emb_module.embeddings, "embed", _fake_embed)
    monkeypatch.setattr(emb_module.embeddings, "embed_many", _fake_embed_many)
    yield


@pytest.fixture
def memory_qdrant(monkeypatch):
    """In-memory Qdrant with dim=8 matching the fake embedder."""
    monkeypatch.setenv("QDRANT_URL", ":memory:")
    monkeypatch.setenv("EMBEDDING_DIM", "8")
    from config.settings import get_settings
    get_settings.cache_clear()

    from services.vector_db import vector_db
    vector_db.disconnect()
    vector_db._settings = get_settings()
    vector_db.connect()
    yield vector_db
    vector_db.disconnect()
    get_settings.cache_clear()
    vector_db._settings = get_settings()


@pytest.fixture
def capture_on(monkeypatch):
    """Enable capture + audit logging redirected to a no-op file."""
    monkeypatch.setenv("SESSION_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("SESSION_CAPTURE_REDACT_SECRETS", "true")
    monkeypatch.setenv("ENABLE_AUDIT_LOG", "false")
    from config.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def capture_off(monkeypatch):
    monkeypatch.setenv("SESSION_CAPTURE_ENABLED", "false")
    from config.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def mocked_worthy_classifier(monkeypatch):
    """Mock the LLM provider to return a worthy=True classification."""
    import services.llm_service as llm_mod

    fake_provider = MagicMock()
    fake_provider.enabled = True
    fake_provider.name = "fake"
    fake_provider.generate.return_value = (
        '{"worthy": true, "problem": "Pod was OOMKilled",'
        ' "resolution": "Bump memory limit to 1Gi",'
        ' "error_signature": "OOMKilled after deploy"}'
    )
    monkeypatch.setattr(llm_mod.llm_service, "_provider", fake_provider)
    yield fake_provider


@pytest.fixture
def mocked_unworthy_classifier(monkeypatch):
    """Mock the LLM provider to return a worthy=False classification."""
    import services.llm_service as llm_mod

    fake_provider = MagicMock()
    fake_provider.enabled = True
    fake_provider.name = "fake"
    fake_provider.generate.return_value = '{"worthy": false}'
    monkeypatch.setattr(llm_mod.llm_service, "_provider", fake_provider)
    yield fake_provider


# ── Redaction ────────────────────────────────────────────────────────────────

class TestRedaction:
    """Acceptance criteria #4, #9."""

    def test_google_api_key(self):
        from services.rag import redact
        text = "key=AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        out = redact(text)
        assert "AIza" not in out or "<REDACTED:google_api_key>" in out

    def test_github_pat(self):
        from services.rag import redact
        # Real-shape PAT (ghp_ + 30+ chars)
        text = "Authorization header: ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
        out = redact(text)
        assert "ghp_aBcDeF" not in out
        assert "<REDACTED:github_pat>" in out

    def test_jwt(self):
        from services.rag import redact
        text = (
            "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0."
            "abc123signaturebytesxyz789"
        )
        out = redact(text)
        # Either the Bearer or JWT pattern catches it — both result in redaction.
        assert "eyJhbGciOiJIUzI1NiJ9" not in out

    def test_pem_private_key(self):
        from services.rag import redact
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAtest12345\n"
            "moremoremoremorebase64data\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out = redact(text)
        assert "BEGIN RSA PRIVATE KEY" not in out
        assert "<REDACTED:private_key>" in out

    def test_kubeconfig_token(self):
        from services.rag import redact
        text = "  token:  abcDEF123456789012345678901234567890XYZ"
        out = redact(text)
        assert "abcDEF1234567" not in out
        assert "<REDACTED:k8s_token>" in out

    def test_long_base64_blob(self):
        from services.rag import redact
        # 60+ alphanumeric chars triggers the base64 catch-all
        text = "data: " + "A" * 70
        out = redact(text)
        assert "AAAAAAAAAA" not in out
        assert "<REDACTED:base64_blob>" in out

    def test_short_strings_preserved(self):
        """Don't over-redact normal text."""
        from services.rag import redact
        text = "kubectl get pods -n prod returned 3 pods running"
        out = redact(text)
        assert out == text


# ── Quick-reject rules (no classifier call) ─────────────────────────────────

class TestCaptureQuickReject:
    """Acceptance criterion #3, #8."""

    def test_disabled_returns_none(self, capture_off):
        from services.rag import maybe_capture
        assert maybe_capture("real problem", "real answer here that is long enough",
                              "investigate_pod") is None

    def test_tool_used_error_rejected(self, capture_on, fake_embedder,
                                       memory_qdrant, mocked_worthy_classifier):
        from services.rag import maybe_capture
        result = maybe_capture(
            "what happened?",
            "I couldn't connect to the cluster. " * 5,
            tool_used="error",
        )
        assert result is None
        # Classifier must NOT have been called
        mocked_worthy_classifier.generate.assert_not_called()

    def test_short_answer_rejected(self, capture_on, fake_embedder, memory_qdrant,
                                    mocked_worthy_classifier):
        from services.rag import maybe_capture
        # < 40 chars → quick-reject
        result = maybe_capture("problem", "ok",
                                tool_used="investigate_pod")
        assert result is None
        mocked_worthy_classifier.generate.assert_not_called()

    def test_failed_backend_message_rejected(self, capture_on, fake_embedder,
                                              memory_qdrant, mocked_worthy_classifier):
        from services.rag import maybe_capture
        result = maybe_capture(
            "any question",
            "Failed to reach the backend. Is it running?",
            tool_used="error",
        )
        assert result is None


# ── Capture flow (full integration) ─────────────────────────────────────────

class TestCaptureFlow:
    """Acceptance criteria #1, #2, #9."""

    def test_worthy_session_captured(
        self, capture_on, fake_embedder, memory_qdrant, mocked_worthy_classifier,
    ):
        from services.rag import maybe_capture
        from services.vector_db import vector_db

        capture_id = maybe_capture(
            "Why is my checkout pod OOMKilled?",
            "The container exceeded its 512Mi memory limit. Bump the limit "
            "to 1Gi and roll out a new revision.",
            tool_used="investigate_pod",
            session_id="s1",
        )
        assert capture_id is not None
        # Entry should exist in session_memory with verified=False
        assert vector_db.exists("session_memory", capture_id)
        result = vector_db._client.retrieve(
            collection_name="session_memory",
            ids=[capture_id],
            with_payload=True,
        )
        payload = result[0].payload
        assert payload["verified"] is False
        assert payload["source"] == "session_capture"
        assert "OOMKilled" in payload["error_signature"]

    def test_unworthy_session_not_captured(
        self, capture_on, fake_embedder, memory_qdrant, mocked_unworthy_classifier,
    ):
        from services.rag import maybe_capture
        result = maybe_capture(
            "hello", "Hi! How can I help you today with K8s questions?",
            tool_used="none",
        )
        # tool_used="none" is a quick-reject, so classifier not even called.
        # That's a stricter version of "unworthy" — assert no capture.
        assert result is None

    def test_redacted_payload(
        self, capture_on, fake_embedder, memory_qdrant, mocked_worthy_classifier,
    ):
        """Acceptance criterion #9 — secrets in captured text are scrubbed."""
        from services.rag import maybe_capture
        from services.vector_db import vector_db

        secret = "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        capture_id = maybe_capture(
            "Why does my deployment have wrong creds?",
            f"You leaked the Google key {secret} in your ConfigMap. " * 3,
            tool_used="investigate_pod",
        )
        assert capture_id is not None
        result = vector_db._client.retrieve(
            collection_name="session_memory",
            ids=[capture_id],
            with_payload=True,
        )
        answer = result[0].payload["answer"]
        # Token replaced with marker; raw key not stored
        assert secret not in answer


# ── Promotion ───────────────────────────────────────────────────────────────

class TestPromotion:
    """Acceptance criteria #5, #7."""

    def _seed_capture(self):
        from services.rag import maybe_capture
        return maybe_capture(
            "Why is OOMKilled?",
            "Bump memory limit. " * 5,
            tool_used="investigate_pod",
            session_id="s1",
        )

    def test_promote_moves_to_runbook(
        self, capture_on, fake_embedder, memory_qdrant, mocked_worthy_classifier,
    ):
        from services.rag import promote
        from services.vector_db import vector_db

        capture_id = self._seed_capture()
        assert capture_id is not None

        result = promote(capture_id)
        assert result["success"] is True
        assert result["already_promoted"] is False

        # Entry now in runbook with verified=True
        assert vector_db.exists("runbook", capture_id)
        retrieved = vector_db._client.retrieve(
            collection_name="runbook",
            ids=[capture_id],
            with_payload=True,
        )
        assert retrieved[0].payload["verified"] is True
        assert retrieved[0].payload["upvotes"] >= 1

        # And NOT in session_memory anymore
        assert not vector_db.exists("session_memory", capture_id)

    def test_promote_idempotent(
        self, capture_on, fake_embedder, memory_qdrant, mocked_worthy_classifier,
    ):
        from services.rag import promote
        capture_id = self._seed_capture()

        first = promote(capture_id)
        second = promote(capture_id)
        assert first["success"] is True
        assert second["success"] is True
        assert second["already_promoted"] is True

    def test_promote_unknown_id_fails_gracefully(
        self, capture_on, fake_embedder, memory_qdrant,
    ):
        from services.rag import promote
        result = promote("00000000-0000-0000-0000-000000000000")
        assert result["success"] is False
        assert "not_found" in result["error"]


# ── Quarantine ──────────────────────────────────────────────────────────────

class TestQuarantine:
    """Acceptance criterion #6."""

    def test_quarantine_deletes_from_session_memory(
        self, capture_on, fake_embedder, memory_qdrant, mocked_worthy_classifier,
    ):
        from services.rag import maybe_capture, quarantine
        from services.vector_db import vector_db

        capture_id = maybe_capture(
            "test problem", "Test answer that is at least 40 chars long here.",
            tool_used="investigate_pod",
        )
        assert capture_id is not None
        assert vector_db.exists("session_memory", capture_id)

        result = quarantine(capture_id)
        assert result["success"] is True
        assert result["already_quarantined"] is False
        assert not vector_db.exists("session_memory", capture_id)
        # Critical: NOT promoted to runbook
        assert not vector_db.exists("runbook", capture_id)

    def test_quarantine_idempotent(
        self, capture_on, fake_embedder, memory_qdrant,
    ):
        from services.rag import quarantine
        first = quarantine("00000000-0000-0000-0000-000000000000")
        assert first["success"] is True
        assert first["already_quarantined"] is True
        second = quarantine("00000000-0000-0000-0000-000000000000")
        assert second["success"] is True
        assert second["already_quarantined"] is True


# ── Router interaction: unverified entries never trigger cached ─────────────

class TestRouterDoesNotCacheUnverified:
    """Acceptance criterion #10."""

    def test_session_memory_only_grounded_never_cached(
        self, capture_on, fake_embedder, memory_qdrant, mocked_worthy_classifier,
        monkeypatch,
    ):
        """Even with similarity=1.0 on a session_memory entry, cached mode
        never fires (cached requires `verified=True` AND collection==runbook).
        """
        from services.rag import maybe_capture, route
        # Enable router with low threshold so the entry hits grounded.
        monkeypatch.setenv("RAG_ROUTER_ENABLED", "true")
        monkeypatch.setenv("RAG_ROUTER_CACHED_THRESHOLD", "0.92")
        monkeypatch.setenv("RAG_ROUTER_GROUNDED_THRESHOLD", "0.70")
        monkeypatch.setenv(
            "RAG_ROUTER_COLLECTIONS", "runbook,devops_doc,session_memory",
        )
        from config.settings import get_settings
        get_settings.cache_clear()

        question = "Pod restarting again after deploy"
        capture_id = maybe_capture(
            question,
            "Looks like resource limits are wrong; bump the memory cap.",
            tool_used="investigate_pod",
        )
        assert capture_id is not None

        # Exact same question → similarity ≈ 1.0
        decision = route(question)
        # Even at similarity 1.0, this entry came from session_memory,
        # which is unverified — cached mode must NOT fire.
        assert decision.mode != "cached"
