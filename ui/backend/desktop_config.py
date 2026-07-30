"""Desktop settings that must survive a restart.

Most of desktop mode's settings live in environment variables, set by
`desktop_main` on each launch and toggled in-process by the settings router.
That works for anything the wizard re-derives every time. It does not work for
something the user types once and expects to still be there tomorrow — an
Alertmanager URL is the first of those.

Kept deliberately small and separate from `desktop_secrets`: this file is
plain configuration, and the keychain is for credentials. It is still written
`0600`, because an Alertmanager URL can carry basic-auth credentials in the
userinfo portion.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

import desktop_paths

logger = logging.getLogger(__name__)

DEFAULTS: Dict[str, Any] = {
    "alertmanager_url": "",
    "notifications_enabled": False,
    # Seconds between polls. Alertmanager is cheap to query and a laptop is
    # not a scrape target, so this is about noticing quickly rather than load.
    "alert_poll_seconds": 30,
}


def load() -> Dict[str, Any]:
    """Read the config, falling back to defaults on anything unreadable.

    A corrupt config must never stop the app from starting — the user would
    have no way to fix it from a window that will not open.
    """
    path = desktop_paths.config_path()
    values = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as stream:
            stored = json.load(stream)
        if isinstance(stored, dict):
            for key in DEFAULTS:
                if key in stored:
                    values[key] = stored[key]
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as error:
        logger.warning("desktop: ignoring unreadable config at %s (%s)", path, error)
    return values


def save(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Merge `updates` into the stored config and return the new state.

    Unknown keys are dropped rather than persisted — this file is read by
    later versions, and silently accumulating junk makes that harder.
    """
    values = load()
    for key, value in updates.items():
        if key in DEFAULTS:
            values[key] = value

    path = desktop_paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same reasoning as desktop_secrets._write_fallback: the mode argument to
    # os.open only applies when the file is created, so an existing file left
    # at 0644 would keep those permissions.
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w") as stream:
            json.dump(values, stream, indent=2)
    except BaseException:
        try:
            os.close(handle)
        except OSError:
            pass
        raise
    return values


def normalize_alertmanager_url(raw: str) -> str:
    """Accept what a user would actually paste, or reject it clearly.

    Returns "" for empty input (meaning "disabled"). Raises ValueError for
    anything that is not a plausible http(s) base URL, so the settings
    endpoint can answer 400 rather than silently never polling.
    """
    value = (raw or "").strip()
    if not value:
        return ""
    if "://" not in value:
        # "localhost:9093" is the shape a port-forward gives you.
        value = f"http://{value}"
    if not value.startswith(("http://", "https://")):
        raise ValueError("Alertmanager URL must start with http:// or https://")

    # Strip trailing slashes from the remainder, not the whole string. Doing
    # it first turned "http://" into "http:", which then failed the "://"
    # test, got a second scheme prepended, and was accepted as
    # "http://http:" — a URL that would never resolve and never say why.
    scheme, _, remainder = value.partition("://")
    remainder = remainder.rstrip("/")
    if not remainder:
        raise ValueError("Alertmanager URL is missing a host")
    return f"{scheme}://{remainder}"
