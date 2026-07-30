"""Desktop-only setup and settings.

Registered in main.py only under `if DESKTOP_MODE`, so a 404 on
`GET /api/desktop/setup` is the frontend's signal that it is talking to a
server-mode deployment.

No per-route auth: desktop_security is already the boundary in this mode, and
there are no user accounts to authorise against.

The governing rule here is **verify before storing**. A bad API key accepted
silently produces an app whose first symptom is a failed investigation
minutes later, with nothing pointing back at the key. Every write is preceded
by a live call to the provider.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import desktop_alerts
import desktop_config
import desktop_secrets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/desktop", tags=["Desktop"])

# Providers that can drive investigations.
KNOWN_PROVIDERS = ("anthropic", "openai", "gemini", "ollama")
# Ollama runs locally and needs no credential.
PROVIDERS_NEEDING_KEY = ("anthropic", "openai", "gemini")
# Providers exposing an embeddings API. Anthropic does not — that absence is
# the entire reason for the optional third wizard step.
EMBEDDING_PROVIDERS = ("voyage", "openai", "gemini")

_PROVIDER_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


# ── models ────────────────────────────────────────────────────────────────


class SetupState(BaseModel):
    configured: bool
    llm_provider: Optional[str] = None
    needs_embeddings_key: bool = False
    memory_available: bool = False
    memory_mode: str = "keyword"  # "vector" | "keyword"
    keychain_secure: bool = True
    keychain_backend: str = ""


class LlmSetupRequest(BaseModel):
    provider: str = Field(..., description="one of KNOWN_PROVIDERS")
    api_key: Optional[str] = None  # omitted for ollama


class LlmSetupResponse(BaseModel):
    ok: bool
    provider: str
    needs_embeddings_key: bool


class EmbeddingsSetupRequest(BaseModel):
    provider: str
    api_key: str


class EmbeddingsSetupResponse(BaseModel):
    ok: bool
    provider: str
    dim: int


class DesktopSettings(BaseModel):
    memory_enabled: bool = True
    remote_diagnostics_enabled: bool = False
    memory_mode: str = "keyword"
    memory_available: bool = False
    keychain_secure: bool = True
    keychain_backend: str = ""
    # Persisted in config.json rather than the environment: the user types
    # these once and expects them to survive a restart.
    alertmanager_url: str = ""
    notifications_enabled: bool = False
    # Which cluster background investigations target. Read-only here: it is
    # set by connecting a cluster, so there is one act of choosing rather
    # than two settings that can disagree.
    default_cluster_context: str = ""
    # Which kubectl is actually being used, and which credential plugins are
    # missing. Both are launch-environment dependent — a GUI launch inherits
    # almost no PATH — so "it works in my terminal" is not evidence.
    kubectl_path: str = ""
    missing_auth_plugins: list[str] = []


class DesktopSettingsUpdate(BaseModel):
    memory_enabled: Optional[bool] = None
    remote_diagnostics_enabled: Optional[bool] = None
    alertmanager_url: Optional[str] = None
    notifications_enabled: Optional[bool] = None


# ── helpers ───────────────────────────────────────────────────────────────


def _settings():
    from config.settings import get_settings

    return get_settings()


def _refresh_settings():
    """Drop the cached Settings so newly-exported env vars take effect.

    `get_settings` is lru_cached, so without this the process keeps using the
    configuration captured at first import.
    """
    from config.settings import get_settings

    get_settings.cache_clear()
    return get_settings()


def _reset_caches() -> None:
    """Re-resolve everything that memoises credentials."""
    _refresh_settings()
    try:
        from services.embeddings import embeddings

        embeddings.reset()
    except Exception:  # pragma: no cover — embeddings optional at this point
        logger.debug("embeddings reset skipped", exc_info=True)


def _kubectl_path() -> str:
    """Absolute path to the kubectl in use, or "" when none was found."""
    try:
        from k8s import binaries

        return binaries.found("kubectl") or ""
    except Exception:  # discovery must never break the settings screen
        logger.debug("kubectl discovery failed", exc_info=True)
        return ""


def _missing_auth_plugins() -> list[str]:
    """Credential plugins a cloud kubeconfig might name that we cannot find."""
    try:
        from k8s import binaries

        return binaries.missing_auth_plugins()
    except Exception:
        logger.debug("auth plugin probe failed", exc_info=True)
        return []


def _stored_llm_provider() -> Optional[str]:
    """Which provider the user configured, if any."""
    configured = _settings().llm_provider
    if configured == "ollama":
        return "ollama"
    for provider in PROVIDERS_NEEDING_KEY:
        if desktop_secrets.has_secret(f"llm.{provider}"):
            if configured == provider:
                return provider
    # A key exists but is not the selected provider — report the selected one
    # only if it is actually usable.
    if configured in PROVIDERS_NEEDING_KEY and desktop_secrets.has_secret(
        f"llm.{configured}"
    ):
        return configured
    return None


def _has_embeddings_key() -> bool:
    return any(
        desktop_secrets.has_secret(f"embeddings.{provider}")
        for provider in EMBEDDING_PROVIDERS
    )


def _memory_available() -> bool:
    try:
        from services.embeddings import embeddings

        return bool(embeddings.available)
    except Exception:
        return False


def _humanize_provider_error(exc: Exception) -> str:
    """Pull the readable sentence out of a provider SDK error.

    Providers raise with their whole JSON body attached, e.g.

        Anthropic request failed: Error code: 401 - {'type': 'error',
        'error': {'type': 'authentication_error', 'message': 'invalid
        x-api-key'}, 'request_id': 'req_011Cd...'}

    On a first-run screen that is noise around one useful phrase. Extract the
    innermost `message`; fall back to the raw text if the shape is unfamiliar,
    since a truncated real error still beats a generic one.
    """
    raw = str(exc).strip()
    match = re.search(r"['\"]message['\"]\s*:\s*['\"](.+?)['\"]", raw)
    if match:
        return match.group(1)
    if len(raw) > 200:
        return raw[:200].rstrip() + "…"
    return raw


def _probe_llm(provider: str, api_key: Optional[str]) -> None:
    """Make a real call with the candidate credential.

    Providers are constructed directly rather than through `get_provider`, so
    nothing global is mutated until the key has proven itself.
    Raises RuntimeError with the provider's own message on failure.
    """
    settings = _settings()

    if provider == "ollama":
        import httpx

        base = (settings.ollama_base_url or "http://localhost:11434").rstrip("/")
        try:
            response = httpx.get(f"{base}/api/tags", timeout=5.0)
            response.raise_for_status()
        except Exception as exc:
            raise RuntimeError(
                f"Could not reach Ollama at {base}. Is it running? "
                f"Start it with `ollama serve`. ({exc})"
            ) from exc
        return

    if provider == "anthropic":
        from services.llm.anthropic_provider import AnthropicProvider

        instance = AnthropicProvider(
            api_key=api_key,
            model=settings.anthropic_model,
            timeout=settings.anthropic_timeout_seconds,
        )
    elif provider == "openai":
        from services.llm.openai_provider import OpenAIProvider

        instance = OpenAIProvider(
            api_key=api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url,
            timeout=settings.openai_timeout_seconds,
        )
    elif provider == "gemini":
        from services.llm import HARDCODED_GEMINI_MODEL
        from services.llm.gemini_provider import GeminiProvider

        instance = GeminiProvider(
            api_key=api_key,
            model=HARDCODED_GEMINI_MODEL,
            timeout=settings.gemini_timeout_seconds,
        )
    else:  # pragma: no cover — guarded by the caller
        raise RuntimeError(f"Unsupported provider {provider!r}")

    # Smallest possible round trip that still proves auth works.
    instance.generate(prompt="ping", max_tokens=1)


def _apply_provider(provider: str) -> None:
    """Make the stored credential the active configuration.

    Persists the choice as well as applying it. Applying it in-process only
    meant the selection lasted until the app was closed, and the next launch
    fell back to the `llm_provider` default with no key — the LLM silently
    went away. `desktop_secrets.restore_to_environ` reads what is recorded
    here at startup.
    """
    os.environ["LLM_PROVIDER"] = provider
    env_name = _PROVIDER_ENV.get(provider)
    if env_name:
        key = desktop_secrets.get_secret(f"llm.{provider}")
        if key:
            os.environ[env_name] = key
    _persist_choice("llm_provider", provider)
    _reset_caches()


def _apply_embeddings(provider: str) -> None:
    os.environ["EMBEDDINGS_MODE"] = "api"
    os.environ["EMBEDDINGS_PROVIDER"] = provider
    key = desktop_secrets.get_secret(f"embeddings.{provider}")
    if key:
        os.environ["EMBEDDINGS_API_KEY"] = key
    _persist_choice("embeddings_provider", provider)
    _reset_caches()


def _persist_choice(key: str, provider: str) -> None:
    """Record a provider selection so the next launch can find its key.

    A failure to persist must not fail the request: the credential is already
    saved and working for this session, and reporting an error would suggest
    it was not.
    """
    try:
        import desktop_config

        desktop_config.save({key: provider})
    except Exception:
        logger.warning("could not persist %s=%s", key, provider, exc_info=True)


# ── endpoints ─────────────────────────────────────────────────────────────


@router.get("/setup", response_model=SetupState)
def get_setup_state() -> SetupState:
    provider = _stored_llm_provider()
    available = _memory_available()
    return SetupState(
        configured=provider is not None,
        llm_provider=provider,
        needs_embeddings_key=provider == "anthropic" and not _has_embeddings_key(),
        memory_available=available,
        memory_mode="vector" if available else "keyword",
        keychain_secure=desktop_secrets.is_secure(),
        keychain_backend=desktop_secrets.backend_name(),
    )


@router.post("/setup/llm", response_model=LlmSetupResponse)
def setup_llm(body: LlmSetupRequest) -> LlmSetupResponse:
    provider = body.provider.strip().lower()
    if provider not in KNOWN_PROVIDERS:
        raise HTTPException(400, f"Unknown provider '{body.provider}'")
    if provider in PROVIDERS_NEEDING_KEY and not (body.api_key or "").strip():
        raise HTTPException(400, f"An API key is required for {provider}")

    api_key = (body.api_key or "").strip() or None
    try:
        _probe_llm(provider, api_key)
    except Exception as exc:
        # Surface the provider's own wording: "invalid x-api-key" is far more
        # actionable than a generic connection failure.
        raise HTTPException(
            400, f"Could not verify {provider}: {_humanize_provider_error(exc)}"
        ) from exc

    if api_key:
        desktop_secrets.set_secret(f"llm.{provider}", api_key)
    _apply_provider(provider)

    return LlmSetupResponse(
        ok=True,
        provider=provider,
        needs_embeddings_key=provider == "anthropic" and not _has_embeddings_key(),
    )


@router.post("/setup/embeddings", response_model=EmbeddingsSetupResponse)
def setup_embeddings(body: EmbeddingsSetupRequest) -> EmbeddingsSetupResponse:
    provider = body.provider.strip().lower()
    if provider not in EMBEDDING_PROVIDERS:
        raise HTTPException(
            400,
            f"'{body.provider}' has no embeddings API. "
            f"Choose one of: {', '.join(EMBEDDING_PROVIDERS)}",
        )
    api_key = body.api_key.strip()
    if not api_key:
        raise HTTPException(400, "An API key is required")

    # Store first so the backend can read it, then verify; roll back on
    # failure. The embeddings backend resolves its key through the keychain,
    # so unlike the LLM probe there is no way to inject a candidate directly.
    previous = desktop_secrets.get_secret(f"embeddings.{provider}")
    desktop_secrets.set_secret(f"embeddings.{provider}", api_key)
    _apply_embeddings(provider)

    try:
        from services.embeddings import embeddings

        vector = embeddings.embed("connection test")
        dim = len(vector)
    except Exception as exc:
        if previous is None:
            desktop_secrets.delete_secret(f"embeddings.{provider}")
        else:
            desktop_secrets.set_secret(f"embeddings.{provider}", previous)
        _reset_caches()
        raise HTTPException(
            400,
            f"Could not verify {provider} embeddings: {_humanize_provider_error(exc)}",
        ) from exc

    return EmbeddingsSetupResponse(ok=True, provider=provider, dim=dim)


@router.get("/settings", response_model=DesktopSettings)
def get_desktop_settings() -> DesktopSettings:
    available = _memory_available()
    stored = desktop_config.load()
    return DesktopSettings(
        memory_enabled=os.environ.get("KUBEASTRA_MEMORY_ENABLED", "1") != "0",
        remote_diagnostics_enabled=os.environ.get(
            "KUBEASTRA_REMOTE_DIAGNOSTICS", "0"
        )
        == "1",
        memory_mode="vector" if available else "keyword",
        memory_available=available,
        keychain_secure=desktop_secrets.is_secure(),
        keychain_backend=desktop_secrets.backend_name(),
        alertmanager_url=str(stored.get("alertmanager_url") or ""),
        notifications_enabled=bool(stored.get("notifications_enabled")),
        default_cluster_context=str(stored.get("default_cluster_context") or ""),
        kubectl_path=_kubectl_path(),
        missing_auth_plugins=_missing_auth_plugins(),
    )


@router.put("/settings", response_model=DesktopSettings)
def update_desktop_settings(body: DesktopSettingsUpdate) -> DesktopSettings:
    if body.memory_enabled is not None:
        os.environ["KUBEASTRA_MEMORY_ENABLED"] = "1" if body.memory_enabled else "0"
    if body.remote_diagnostics_enabled is not None:
        os.environ["KUBEASTRA_REMOTE_DIAGNOSTICS"] = (
            "1" if body.remote_diagnostics_enabled else "0"
        )

    updates: dict = {}
    if body.alertmanager_url is not None:
        try:
            updates["alertmanager_url"] = desktop_config.normalize_alertmanager_url(
                body.alertmanager_url
            )
        except ValueError as error:
            # Reject rather than store something that would silently never
            # poll — a notifications feature that quietly does nothing is
            # worse than one that refuses to be configured.
            raise HTTPException(status_code=400, detail=str(error)) from error
    if body.notifications_enabled is not None:
        updates["notifications_enabled"] = bool(body.notifications_enabled)

    if updates:
        merged = desktop_config.save(updates)
        # Enabling with no URL configured cannot work; say so now.
        if merged.get("notifications_enabled") and not merged.get("alertmanager_url"):
            desktop_config.save({"notifications_enabled": False})
            raise HTTPException(
                status_code=400,
                detail="Set an Alertmanager URL before enabling notifications.",
            )
        desktop_alerts.poller.start()
        # Poll now rather than in up to a full interval. Enabling should
        # prime against the cluster as it is at this moment; otherwise an
        # alert that starts firing in the gap is written off as pre-existing.
        desktop_alerts.poller.refresh()

    _reset_caches()
    return get_desktop_settings()


@router.get("/notifications")
def drain_notifications() -> dict:
    """Alerts that have started firing since the last call.

    Polled by the Tauri shell, which raises one native notification per item.
    Draining is destructive — see AlertPoller.drain for why re-delivery would
    be worse than an occasional loss.
    """
    return {
        "alerts": desktop_alerts.poller.drain(),
        "status": desktop_alerts.poller.status(),
    }


@router.post("/notifications/test")
def test_alertmanager(body: DesktopSettingsUpdate) -> dict:
    """Check a URL before the user commits to it.

    Same verify-before-store rule the LLM and embeddings steps follow: a
    settings screen that accepts anything and fails silently in a background
    thread is untestable by the person using it.
    """
    try:
        url = desktop_config.normalize_alertmanager_url(body.alertmanager_url or "")
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not url:
        raise HTTPException(status_code=400, detail="No Alertmanager URL given.")

    try:
        alerts = desktop_alerts.poller.fetch(url, timeout=8.0)
    except Exception as error:
        raise HTTPException(
            status_code=400, detail=f"Could not reach Alertmanager: {error}"
        ) from error
    return {"ok": True, "url": url, "firing": len(alerts)}


@router.delete("/secrets/{name}")
def forget_secret(name: str) -> dict:
    # Only names this app owns; prevents path-ish or arbitrary keyring access.
    valid = {f"llm.{p}" for p in PROVIDERS_NEEDING_KEY} | {
        f"embeddings.{p}" for p in EMBEDDING_PROVIDERS
    }
    if name not in valid:
        raise HTTPException(404, f"Unknown secret '{name}'")
    desktop_secrets.delete_secret(name)

    # Forget the recorded choice too, and clear the key out of the running
    # process. Leaving either behind means the app still believes it is
    # configured for a provider whose credential no longer exists — which is
    # the state this endpoint exists to escape.
    if name.startswith("llm."):
        provider = name.split(".", 1)[1]
        _persist_choice("llm_provider", "")
        env_name = _PROVIDER_ENV.get(provider)
        if env_name:
            os.environ.pop(env_name, None)
    elif name.startswith("embeddings."):
        _persist_choice("embeddings_provider", "")
        os.environ.pop("EMBEDDINGS_API_KEY", None)

    _reset_caches()
    return {"ok": True}
