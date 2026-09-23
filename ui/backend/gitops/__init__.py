"""GitOps PR proposals. Token resolution differs by run mode."""
from __future__ import annotations

import os

_KEYCHAIN_NAME = "gitops.github.pat"


def resolve_token() -> str | None:
    """Desktop reads the PAT from the OS keychain; server from settings/env.

    The `config.settings` import is deferred into the function so the pure
    pipeline modules (locate, edit, index, ...) stay importable with only the
    backend dir on the path — importing the package must not require mcp/.
    """
    if (os.environ.get("KUBEASTRA_MODE") or "").lower() == "desktop":
        try:
            import desktop_secrets
            return desktop_secrets.get_secret(_KEYCHAIN_NAME)
        except Exception:
            return None
    from config.settings import get_settings
    return get_settings().gitops_github_token or None
