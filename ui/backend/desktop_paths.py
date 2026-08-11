"""Filesystem layout for desktop mode.

Everything the desktop app writes lives under one directory, so a user can
back it up or delete it in one action, and an uninstall has a single thing to
remove.

    macOS    ~/Library/Application Support/KubeAstra/
    Linux    $XDG_STATE_HOME/kubeastra/   (default ~/.local/state/kubeastra/)
    Windows  %APPDATA%\\KubeAstra\\

      kubeastra.db     SQLite: sessions, messages, cluster connections
      vectors/         qdrant-client local storage (investigation memory)
      kubeconfigs/     kubeconfigs pasted into the UI
      logs/            audit log
      desktop.json     single-instance lockfile (written by the CLI launcher)
      secrets.json     ONLY when the OS has no usable keychain (see
                       desktop_secrets); 0600

This deliberately mirrors `cli/src/kubeastra/desktop.py::state_dir()` rather
than importing it: the CLI is an optional frontend and the backend must not
depend on it. `tests/test_desktop_paths.py` asserts the two agree.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "KubeAstra"


def state_dir() -> Path:
    """Root of the app's data directory. Does not create anything."""
    override = os.environ.get("KUBEASTRA_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_DIR_NAME
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "kubeastra"


def db_path() -> Path:
    return state_dir() / "kubeastra.db"


def vectors_path() -> Path:
    return state_dir() / "vectors"


def kubeconfig_dir() -> Path:
    return state_dir() / "kubeconfigs"


def logs_dir() -> Path:
    return state_dir() / "logs"


def audit_log_path() -> Path:
    return logs_dir() / "audit.log"


def secrets_path() -> Path:
    return state_dir() / "secrets.json"


def config_path() -> Path:
    """Settings that must outlive the process.

    Most desktop settings live in environment variables, which is fine for
    ones the wizard re-derives on every launch. An Alertmanager URL is not
    one of those — a user who configures it once expects it to still be there
    tomorrow.
    """
    return state_dir() / "config.json"


def ensure_layout() -> Path:
    """Create the directory tree. Idempotent; safe to call on every launch.

    Directories are 0700 because kubeconfigs live here — on a shared machine
    another user must not be able to list or read them. On Windows the mode
    argument is ignored and ACL inheritance applies instead.
    """
    root = state_dir()
    for directory in (root, vectors_path(), kubeconfig_dir(), logs_dir()):
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            try:
                directory.chmod(0o700)
            except OSError:
                # A pre-existing directory owned by someone else, or an
                # exotic filesystem. Not fatal — the app still works, it is
                # just not locked down. Callers that store secrets check
                # their own file modes.
                pass
    return root
