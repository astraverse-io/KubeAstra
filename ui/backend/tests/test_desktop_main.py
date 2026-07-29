"""Desktop entry point: the parent-process contract.

Both behaviours here were found by the Tauri spike, not by review:

  * the backend died with BrokenPipeError when the parent stopped reading
    stdout after the handshake;
  * the backend outlived a force-quit parent, keeping the port and the
    vector store's exclusive lock, which blocks the next launch.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import desktop_main  # noqa: E402


# ── announce(): stdout must never be fatal ────────────────────────────────


def test_announce_writes_a_line(capsys):
    desktop_main.announce("PORT=1234")
    assert capsys.readouterr().out.strip() == "PORT=1234"


def test_announce_survives_a_closed_pipe(monkeypatch):
    """Regression: the Rust side stopped reading after the handshake and the
    next print killed an otherwise healthy backend."""

    def closed_pipe(*args, **kwargs):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr("builtins.print", closed_pipe)
    desktop_main.announce("READY")  # must not raise


def test_announce_survives_a_closed_file(monkeypatch):
    def closed_file(*args, **kwargs):
        raise ValueError("I/O operation on closed file")

    monkeypatch.setattr("builtins.print", closed_file)
    desktop_main.announce("READY")  # must not raise


# ── watch_parent(): no orphans ────────────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="ppid reparenting is POSIX-only")
def test_watchdog_skipped_when_already_orphaned(monkeypatch):
    """nohup / daemonised / direct runs have no parent to outlive — starting
    the watchdog there would exit immediately."""
    started = []
    monkeypatch.setattr(desktop_main.os, "getppid", lambda: 1)
    monkeypatch.setattr(
        desktop_main.threading, "Thread", lambda **kw: started.append(kw) or _NoopThread()
    )
    desktop_main.watch_parent()
    assert started == []


class _NoopThread:
    def start(self):  # pragma: no cover — only reached on failure
        raise AssertionError("watchdog should not have started")


@pytest.mark.skipif(os.name == "nt", reason="ppid reparenting is POSIX-only")
def test_watchdog_starts_when_a_parent_exists(monkeypatch):
    created = {}

    class FakeThread:
        def __init__(self, **kwargs):
            created.update(kwargs)

        def start(self):
            created["started"] = True

    monkeypatch.setattr(desktop_main.os, "getppid", lambda: 4242)
    monkeypatch.setattr(desktop_main.threading, "Thread", FakeThread)
    desktop_main.watch_parent()

    assert created.get("started") is True
    assert created.get("daemon") is True, "watchdog must not block interpreter exit"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover — still running, just not ours
        return True
    return True


@pytest.mark.skipif(os.name == "nt", reason="ppid reparenting is POSIX-only")
def test_child_exits_when_its_parent_is_sigkilled(tmp_path):
    """The case no exit handler can cover: parent killed with SIGKILL.

    This is what the spike caught — a backend outliving its shell, holding the
    port and the vector store's exclusive lock so the next launch cannot
    start.
    """
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import os, sys, time\n"
        f"sys.path.insert(0, {str(BACKEND_DIR)!r})\n"
        "import desktop_main\n"
        "desktop_main.watch_parent(poll_seconds=0.2)\n"
        "print(os.getpid(), flush=True)\n"
        "time.sleep(120)\n"
    )
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, {str(child_script)!r}],\n"
        "                         stdout=subprocess.PIPE, text=True)\n"
        "print(child.stdout.readline().strip(), flush=True)\n"
        "time.sleep(120)\n"
    )

    parent = subprocess.Popen(
        [sys.executable, str(parent_script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        child_pid = int(parent.stdout.readline().strip())
        assert _pid_alive(child_pid), "child should be running before we kill the parent"

        parent.kill()
        parent.wait(timeout=10)

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and _pid_alive(child_pid):
            time.sleep(0.2)

        assert not _pid_alive(child_pid), (
            f"pid {child_pid} outlived its SIGKILLed parent — it would hold "
            f"the vector-store lock and block the next launch"
        )
    finally:
        if parent.poll() is None:  # pragma: no cover
            parent.kill()
