"""GitOps PR proposals. Token resolution differs by run mode."""
from __future__ import annotations

import os

from config.settings import get_settings

_KEYCHAIN_NAME = "gitops.github.pat"


def resolve_token() -> str | None:
    """Desktop reads the PAT from the OS keychain; server from settings/env."""
    if (os.environ.get("KUBEASTRA_MODE") or "").lower() == "desktop":
        try:
            import desktop_secrets
            return desktop_secrets.get_secret(_KEYCHAIN_NAME)
        except Exception:
            return None
    return get_settings().gitops_github_token or None
