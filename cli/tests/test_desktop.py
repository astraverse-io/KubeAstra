"""`kubeastra open` launcher helpers.

The spawn path itself is covered by manual end-to-end runs; these cover the
single-instance bookkeeping, which is the part with real failure modes (a
stale lockfile must never block a fresh launch, and the token must not land
in a world-readable file).
"""

import json
import os
import stat

import pytest

from kubeastra import desktop


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBEASTRA_STATE_DIR", str(tmp_path))
    return tmp_path


def test_state_dir_respects_override(state):
    assert desktop.state_dir() == state


def test_no_instance_when_lockfile_absent(state):
    assert desktop.read_running_instance() is None


def test_stale_lockfile_is_removed(state):
    """A crashed backend leaves a lockfile behind; it must not wedge the app."""
    desktop.lockfile().write_text(json.dumps({"pid": 999999, "port": 1, "token": "x"}))
    assert desktop.read_running_instance() is None
    assert not desktop.lockfile().exists()


def test_corrupt_lockfile_is_ignored(state):
    desktop.lockfile().write_text("not json{")
    assert desktop.read_running_instance() is None


def test_live_pid_without_healthy_backend_is_cleaned(state):
    """Our own PID is alive, but nothing is serving /health on that port."""
    desktop.write_instance(os.getpid(), 9, "token")
    assert desktop.read_running_instance() is None
    assert not desktop.lockfile().exists()


def test_write_instance_keeps_token_private(state):
    desktop.write_instance(os.getpid(), 8080, "secret-token")
    mode = stat.S_IMODE(desktop.lockfile().stat().st_mode)
    assert mode == 0o600, f"lockfile holds the auth token; got {oct(mode)}"


def test_health_ok_false_for_dead_port(state):
    assert desktop.health_ok(9) is False


def test_health_ok_false_without_port(state):
    assert desktop.health_ok(None) is False


def test_backend_entrypoint_override_must_exist(state, monkeypatch, tmp_path):
    monkeypatch.setenv("KUBEASTRA_BACKEND_ENTRY", str(tmp_path / "nope.py"))
    assert desktop.backend_entrypoint() is None

    real = tmp_path / "desktop_main.py"
    real.write_text("")
    monkeypatch.setenv("KUBEASTRA_BACKEND_ENTRY", str(real))
    assert desktop.backend_entrypoint() == real
