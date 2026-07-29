"""Desktop app-data layout.

The directory holds kubeconfigs and (on systems with no keychain) API keys, so
the permission assertions here are load-bearing, not cosmetic.
"""

import os
import stat
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import desktop_paths  # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBEASTRA_STATE_DIR", str(tmp_path))
    return tmp_path


def test_override_wins(state):
    assert desktop_paths.state_dir() == state


def test_paths_sit_under_state_dir(state):
    assert desktop_paths.db_path() == state / "kubeastra.db"
    assert desktop_paths.vectors_path() == state / "vectors"
    assert desktop_paths.kubeconfig_dir() == state / "kubeconfigs"
    assert desktop_paths.audit_log_path() == state / "logs" / "audit.log"
    assert desktop_paths.secrets_path() == state / "secrets.json"


def test_ensure_layout_creates_tree(state):
    desktop_paths.ensure_layout()
    for path in (
        desktop_paths.vectors_path(),
        desktop_paths.kubeconfig_dir(),
        desktop_paths.logs_dir(),
    ):
        assert path.is_dir(), f"{path} was not created"


def test_ensure_layout_is_idempotent(state):
    desktop_paths.ensure_layout()
    marker = desktop_paths.vectors_path() / "keep.txt"
    marker.write_text("data")
    desktop_paths.ensure_layout()
    assert marker.read_text() == "data", "second call clobbered existing data"


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes do not apply on Windows")
def test_directories_are_private(state):
    desktop_paths.ensure_layout()
    for path in (desktop_paths.state_dir(), desktop_paths.kubeconfig_dir()):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o700, f"{path} is {oct(mode)}; kubeconfigs live here"


def test_platform_defaults_without_override(monkeypatch):
    """No override => a per-user location, never the CWD or a shared dir."""
    monkeypatch.delenv("KUBEASTRA_STATE_DIR", raising=False)
    resolved = desktop_paths.state_dir()
    assert resolved.is_absolute()
    assert str(resolved).startswith(str(Path.home())) or os.name == "nt"


def test_agrees_with_cli_launcher(state):
    """The CLI duplicates this layout (it must not import the backend).

    If these drift, `kubeastra open` and the backend disagree about where data
    lives and the app silently starts with an empty history.
    """
    cli_src = BACKEND_DIR.parent.parent / "cli" / "src"
    if not cli_src.is_dir():  # pragma: no cover — source checkout only
        pytest.skip("CLI package not present")
    sys.path.insert(0, str(cli_src))
    try:
        from kubeastra import desktop as cli_desktop
    finally:
        sys.path.remove(str(cli_src))
    assert cli_desktop.state_dir() == desktop_paths.state_dir()
