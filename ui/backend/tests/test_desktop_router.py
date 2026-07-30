"""First-run wizard API.

The property that matters most: a credential is never persisted before it has
been proven to work. A silently-stored bad key produces an app whose first
symptom is a failed investigation with nothing pointing back at the cause.
"""

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
# main.py puts mcp/ on the path at import time; these tests build a bare app,
# so `services.*` has to be made importable here.
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import desktop_secrets  # noqa: E402
from routers import desktop as desktop_router  # noqa: E402


class _MemoryKeyring:
    __module__ = "keyring.backends.macOS"

    def __init__(self):
        self.store = {}

    def get_keyring(self):
        return self

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def get_password(self, service, name):
        return self.store.get((service, name))

    def delete_password(self, service, name):
        self.store.pop((service, name), None)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBEASTRA_STATE_DIR", str(tmp_path))
    # desktop_secrets caches reads for the life of the process, because each
    # one is a potential macOS "allow access?" prompt. Writes through the
    # module invalidate it — the only writer in production — but swapping the
    # keyring underneath it here is not a write, so entries from the previous
    # test would survive into this one's fresh store.
    desktop_secrets.clear_cache()
    # One instance for the whole test: a fresh keyring per call would discard
    # everything written, making storage assertions vacuously fail.
    fake_keyring = _MemoryKeyring()
    monkeypatch.setattr(desktop_secrets, "_keyring", lambda: fake_keyring)
    # Never let a test touch a real provider.
    monkeypatch.setattr(desktop_router, "_probe_llm", lambda provider, key: None)
    monkeypatch.setattr(desktop_router, "_reset_caches", lambda: None)
    monkeypatch.setattr(desktop_router, "_apply_provider", lambda provider: None)

    app = FastAPI()
    app.include_router(desktop_router.router, prefix="/api")
    return TestClient(app)


# ── setup state ───────────────────────────────────────────────────────────


def test_fresh_install_is_unconfigured(client, monkeypatch):
    monkeypatch.setattr(desktop_router, "_stored_llm_provider", lambda: None)
    body = client.get("/api/desktop/setup").json()
    assert body["configured"] is False
    assert body["llm_provider"] is None


def test_setup_state_reports_keychain_health(client):
    body = client.get("/api/desktop/setup").json()
    assert body["keychain_secure"] is True
    assert body["keychain_backend"] == "_MemoryKeyring"


def test_setup_state_never_returns_a_key(client):
    desktop_secrets.set_secret("llm.openai", "sk-super-secret")
    raw = client.get("/api/desktop/setup").text
    assert "sk-super-secret" not in raw


# ── storing an LLM provider ───────────────────────────────────────────────


def test_valid_key_is_stored(client):
    response = client.post(
        "/api/desktop/setup/llm", json={"provider": "openai", "api_key": "sk-good"}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert desktop_secrets.get_secret("llm.openai") == "sk-good"


def test_failed_probe_does_not_store_the_key(client, monkeypatch):
    """The core guarantee of this router."""

    def reject(provider, key):
        raise RuntimeError("401 invalid x-api-key")

    monkeypatch.setattr(desktop_router, "_probe_llm", reject)

    response = client.post(
        "/api/desktop/setup/llm", json={"provider": "openai", "api_key": "sk-bad"}
    )
    assert response.status_code == 400
    assert desktop_secrets.get_secret("llm.openai") is None


def test_probe_failure_surfaces_provider_message(client, monkeypatch):
    """"invalid x-api-key" is actionable; "connection failed" is not."""

    def reject(provider, key):
        raise RuntimeError("invalid x-api-key")

    monkeypatch.setattr(desktop_router, "_probe_llm", reject)
    response = client.post(
        "/api/desktop/setup/llm", json={"provider": "anthropic", "api_key": "x"}
    )
    assert "invalid x-api-key" in response.json()["detail"]


def test_unknown_provider_rejected(client):
    response = client.post(
        "/api/desktop/setup/llm", json={"provider": "hal9000", "api_key": "x"}
    )
    assert response.status_code == 400


def test_missing_key_rejected_for_key_providers(client):
    response = client.post("/api/desktop/setup/llm", json={"provider": "openai"})
    assert response.status_code == 400


def test_ollama_needs_no_key(client):
    response = client.post("/api/desktop/setup/llm", json={"provider": "ollama"})
    assert response.status_code == 200
    assert desktop_secrets.get_secret("llm.ollama") is None


def test_anthropic_requests_an_embeddings_key(client):
    """Anthropic publishes no embeddings API — the wizard must say so."""
    response = client.post(
        "/api/desktop/setup/llm", json={"provider": "anthropic", "api_key": "sk-ant"}
    )
    assert response.json()["needs_embeddings_key"] is True


def test_anthropic_with_embeddings_key_does_not_ask_again(client):
    desktop_secrets.set_secret("embeddings.voyage", "vk")
    response = client.post(
        "/api/desktop/setup/llm", json={"provider": "anthropic", "api_key": "sk-ant"}
    )
    assert response.json()["needs_embeddings_key"] is False


def test_openai_does_not_need_a_separate_embeddings_key(client):
    response = client.post(
        "/api/desktop/setup/llm", json={"provider": "openai", "api_key": "sk"}
    )
    assert response.json()["needs_embeddings_key"] is False


# ── embeddings ────────────────────────────────────────────────────────────


def test_embeddings_rejects_provider_without_api(client):
    response = client.post(
        "/api/desktop/setup/embeddings",
        json={"provider": "anthropic", "api_key": "x"},
    )
    assert response.status_code == 400
    assert "no embeddings api" in response.json()["detail"].lower()


def test_embeddings_failure_rolls_back_to_previous_key(client, monkeypatch):
    desktop_secrets.set_secret("embeddings.voyage", "old-key")
    monkeypatch.setattr(desktop_router, "_apply_embeddings", lambda provider: None)

    import services.embeddings as embeddings_module

    def boom(_text):
        raise RuntimeError("401 unauthorized")

    monkeypatch.setattr(embeddings_module.embeddings, "embed", boom)

    response = client.post(
        "/api/desktop/setup/embeddings",
        json={"provider": "voyage", "api_key": "new-bad-key"},
    )
    assert response.status_code == 400
    assert desktop_secrets.get_secret("embeddings.voyage") == "old-key"


def test_embeddings_failure_clears_key_when_none_existed(client, monkeypatch):
    monkeypatch.setattr(desktop_router, "_apply_embeddings", lambda provider: None)

    import services.embeddings as embeddings_module

    monkeypatch.setattr(
        embeddings_module.embeddings,
        "embed",
        lambda _t: (_ for _ in ()).throw(RuntimeError("bad")),
    )

    client.post(
        "/api/desktop/setup/embeddings",
        json={"provider": "voyage", "api_key": "bad"},
    )
    assert desktop_secrets.get_secret("embeddings.voyage") is None


# ── settings + secret removal ─────────────────────────────────────────────


def test_settings_round_trip(client):
    updated = client.put(
        "/api/desktop/settings", json={"remote_diagnostics_enabled": True}
    ).json()
    assert updated["remote_diagnostics_enabled"] is True
    assert client.get("/api/desktop/settings").json()["remote_diagnostics_enabled"] is True


def test_forget_secret(client):
    desktop_secrets.set_secret("llm.openai", "sk")
    assert client.delete("/api/desktop/secrets/llm.openai").status_code == 200
    assert desktop_secrets.get_secret("llm.openai") is None


def test_forget_rejects_unknown_names(client):
    """Stops arbitrary keyring access through this endpoint."""
    assert client.delete("/api/desktop/secrets/../../etc/passwd").status_code in (404, 400)
    assert client.delete("/api/desktop/secrets/llm.evil").status_code == 404
