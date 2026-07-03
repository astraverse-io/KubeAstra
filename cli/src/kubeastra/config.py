"""CLI config — persisted at ``~/.config/kubeastra/config.toml``.

Only two things live here for now: the backend URL and an optional bearer
token. Both can be overridden per-invocation with ``--backend-url`` and
``--api-token`` flags on any command.

XDG conventions are followed on Linux/macOS (``$XDG_CONFIG_HOME`` when
set, otherwise ``~/.config``). Windows uses ``%APPDATA%\\kubeastra``.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import tomli_w

DEFAULT_BACKEND_URL = "http://localhost:8000"


def _config_dir() -> Path:
    """Resolve the config directory, honoring XDG on POSIX + %APPDATA% on Windows."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "kubeastra"
        return Path.home() / "AppData" / "Roaming" / "kubeastra"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "kubeastra"
    return Path.home() / ".config" / "kubeastra"


def config_path() -> Path:
    return _config_dir() / "config.toml"


@dataclass
class Config:
    backend_url: str = DEFAULT_BACKEND_URL
    api_token: Optional[str] = None
    session_id: Optional[str] = None  # persisted per install so history follows the user

    @classmethod
    def load(cls) -> "Config":
        path = config_path()
        if not path.exists():
            return cls()
        try:
            with path.open("rb") as fh:
                data: dict[str, Any] = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            # Corrupt or unreadable config is not fatal — fall back to defaults
            # and let a subsequent `kubeastra config set` overwrite it.
            return cls()
        return cls(
            backend_url=str(data.get("backend_url") or DEFAULT_BACKEND_URL),
            api_token=data.get("api_token") or None,
            session_id=data.get("session_id") or None,
        )

    def save(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Filter Nones so the file stays readable and unset keys don't get
        # persisted as empty strings.
        payload = {k: v for k, v in asdict(self).items() if v is not None and v != ""}
        with path.open("wb") as fh:
            tomli_w.dump(payload, fh)


ALLOWED_KEYS = {"backend_url", "api_token", "session_id"}
