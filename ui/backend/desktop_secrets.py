"""API-key storage for desktop mode, backed by the OS keychain.

Never `.env`, never SQLite. A desktop app's config directory is readable by
anything running as that user, gets swept into Time Machine / Dropbox, and
survives uninstall. The OS keychain is the only place a laptop app should put
a credential.

Key names are namespaced:
    llm.<provider>          e.g. llm.anthropic, llm.openai
    embeddings.<provider>   e.g. embeddings.voyage

If the OS has no usable secret store — a headless Linux box with no Secret
Service daemon is the common case — `keyring` silently resolves to a backend
that either fails or writes plaintext. We detect that, fall back to a 0600
file, and report it so the UI can warn. Degrading silently into a
plaintext-but-looks-fine keyring is the one outcome this module must prevent.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import desktop_paths

logger = logging.getLogger(__name__)

SERVICE = "io.astraverse.kubeastra"

# Substrings identifying keyring backends that do not actually protect
# anything: `fail` is what resolves with no OS store available, and the
# plaintext/file/null backends store credentials in the clear.
_INSECURE_MARKERS = ("fail", "plaintext", "file", "null")

# Namespaces probed by list_configured(). keyring cannot enumerate a service's
# entries portably, so discovery means asking for the names we might have set.
LLM_PROVIDERS = ("anthropic", "openai", "gemini", "ollama")
EMBEDDING_PROVIDERS = ("voyage", "openai", "gemini")


def _keyring():
    import keyring

    return keyring


def backend_name() -> str:
    """Human-readable backend name, for the settings UI."""
    try:
        return type(_keyring().get_keyring()).__name__
    except Exception as exc:  # keyring can raise on import on odd systems
        logger.warning("keyring unavailable: %s", exc)
        return "unavailable"


def is_secure() -> bool:
    """True when the OS keychain will actually protect a stored secret.

    False means callers are using the file fallback and the UI must say so.
    """
    try:
        module = type(_keyring().get_keyring()).__module__.lower()
    except Exception:
        return False
    return not any(marker in module for marker in _INSECURE_MARKERS)


# ── file fallback ─────────────────────────────────────────────────────────
# Only used when is_secure() is False.


def _read_fallback() -> dict:
    path = desktop_paths.secrets_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        logger.warning("%s is unreadable; treating as empty", path)
        return {}
    return data if isinstance(data, dict) else {}


def _write_fallback(data: dict) -> None:
    path = desktop_paths.secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Mode on os.open only applies when the file is CREATED. A secrets.json
    # left behind at 0644 by an older build, or restored from a backup that
    # did not preserve modes, would otherwise keep those permissions and we
    # would write a plaintext key into a world-readable file. fchmod on the
    # open descriptor fixes existing files without a TOCTOU window.
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w") as stream:
            json.dump(data, stream)
    except BaseException:
        # fdopen takes ownership of the descriptor on success; close it
        # ourselves only if we failed before that point.
        try:
            os.close(handle)
        except OSError:
            pass
        raise


# ── public API ────────────────────────────────────────────────────────────


# Read-through cache, process lifetime.
#
# Every keychain read is a potential "allow access?" prompt, and startup used
# to make four of them: `_resolve_provider()` probes each provider it might
# have a key for, then the winner is read again. On a build whose signature
# changes — an ad-hoc signed development build, where identity is derived from
# the binary hash — macOS treats each launch as a new application and asks
# again for every one of them.
#
# A secret written or deleted through this module invalidates its entry. One
# edited *outside* the process (Keychain Access) is not noticed until restart,
# which is the right trade: the alternative is prompting on every read.
_secret_cache: Dict[str, Optional[str]] = {}


def set_secret(name: str, value: str) -> None:
    _secret_cache[name] = value
    if is_secure():
        _keyring().set_password(SERVICE, name, value)
        return
    data = _read_fallback()
    data[name] = value
    _write_fallback(data)


def get_secret(name: str) -> Optional[str]:
    if name in _secret_cache:
        return _secret_cache[name]

    if is_secure():
        try:
            value = _keyring().get_password(SERVICE, name)
        except Exception as exc:
            logger.warning("keyring read failed for %s: %s", name, exc)
            return None  # not cached: a transient failure must be retryable
    else:
        value = _read_fallback().get(name)

    _secret_cache[name] = value
    return value


def delete_secret(name: str) -> None:
    _secret_cache.pop(name, None)
    if is_secure():
        try:
            _keyring().delete_password(SERVICE, name)
        except Exception:
            # keyring raises when the entry is absent; deleting a
            # non-existent secret is not an error for callers.
            pass
        return
    data = _read_fallback()
    data.pop(name, None)
    _write_fallback(data)


def clear_cache() -> None:
    """Forget cached reads. For tests, and after an external key change."""
    _secret_cache.clear()


def list_configured() -> list[str]:
    """Names of stored secrets. Never returns values.

    Lets the API report configuration state without any secret reaching a
    response body.
    """
    if not is_secure():
        return sorted(_read_fallback().keys())

    candidates = [f"llm.{provider}" for provider in LLM_PROVIDERS]
    candidates += [f"embeddings.{provider}" for provider in EMBEDDING_PROVIDERS]
    return [name for name in candidates if get_secret(name)]


def has_secret(name: str) -> bool:
    return bool(get_secret(name))


# Provider -> the env var `mcp/config/settings.py` reads its key from.
_PROVIDER_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def _resolve_provider() -> Optional[str]:
    """Which LLM provider this install is configured for.

    Prefers the recorded choice. Installs made before that was persisted have
    no record, so fall back to whichever credential is actually stored — that
    is the only evidence left of what the user set up.
    """
    import desktop_config

    recorded = (desktop_config.load().get("llm_provider") or "").strip().lower()
    if recorded in LLM_PROVIDERS:
        return recorded

    # Nothing recorded — an install predating that field. Probe, but probe the
    # likeliest first and stop there: each miss is a separate key name, so a
    # blind sweep of all three is three keychain prompts rather than one.
    # Ollama needs no credential and so can never be inferred; only an
    # explicit record selects it.
    preferred = (os.environ.get("LLM_PROVIDER") or "gemini").lower()
    order = [preferred] if preferred in _PROVIDER_ENV else []
    order += [p for p in _PROVIDER_ENV if p != preferred]

    for provider in order:
        if has_secret(f"llm.{provider}"):
            # Record it, so the probe happens once per install rather than
            # once per launch.
            try:
                desktop_config.save({"llm_provider": provider})
            except Exception as error:
                logger.debug("could not record inferred provider: %s", error)
            return provider
    return None


def restore_to_environ() -> Optional[str]:
    """Put stored credentials back into the environment. Returns the provider.

    Desktop mode keeps credentials in the keychain but every consumer reads
    them from the environment via pydantic-settings, and that bridge only ever
    existed inside the setup wizard's save handler. So a key survived a
    restart in the keychain while the process that needed it started blank —
    `GeminiProvider.enabled` went False, and chat silently downgraded to
    single-shot: tools still ran, but no reasoning trace and no synthesis. A
    bundled `mcp/.env` masked this until it was (correctly) removed.

    Called from `desktop_main` before the app is imported, because settings
    are read and memoised at import time.

    An env var already set wins — a developer exporting a key in their shell
    is being explicit. Returning None is a normal state, not an error: a
    first-run install has no credential yet and must still start so the wizard
    can be reached.
    """
    provider = _resolve_provider()
    if not provider:
        return None

    os.environ.setdefault("LLM_PROVIDER", provider)

    env_name = _PROVIDER_ENV.get(provider)
    if env_name and not os.environ.get(env_name):
        key = get_secret(f"llm.{provider}")
        if key:
            os.environ[env_name] = key

    import desktop_config

    embeddings = (desktop_config.load().get("embeddings_provider") or "").strip().lower()
    if embeddings in EMBEDDING_PROVIDERS and not os.environ.get("EMBEDDINGS_API_KEY"):
        key = get_secret(f"embeddings.{embeddings}")
        if key:
            os.environ.setdefault("EMBEDDINGS_MODE", "api")
            os.environ.setdefault("EMBEDDINGS_PROVIDER", embeddings)
            os.environ["EMBEDDINGS_API_KEY"] = key

    return provider
