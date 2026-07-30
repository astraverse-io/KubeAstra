"""Finding kubectl when PATH does not contain it.

A GUI launch starts from launchd with roughly /usr/bin:/bin:/usr/sbin:/sbin.
Every place kubectl actually installs — Docker Desktop's bundle, Rancher's
~/.rd/bin, Homebrew — is missing from that, so `shutil.which` returned None
and the app reported "kubectl binary not found" on machines with a working
kubectl. These cover the resolution that replaced it.
"""

import os
import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[3] / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from k8s import binaries  # noqa: E402


@pytest.fixture(autouse=True)
def clear_cache():
    binaries.reset_cache()
    yield
    binaries.reset_cache()


def _make_executable(directory: Path, name: str = "kubectl") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


# ── the bug this exists for ───────────────────────────────────────────────


def test_finds_kubectl_that_is_not_on_path(tmp_path, monkeypatch):
    """The GUI-launch case: installed, but invisible to `which`."""
    docker_bin = tmp_path / "Docker.app" / "Contents" / "Resources" / "bin"
    expected = _make_executable(docker_bin)

    monkeypatch.setattr(binaries.shutil, "which", lambda _: None)
    monkeypatch.setattr(binaries, "_candidate_dirs", lambda: [docker_bin])

    assert binaries.resolve("kubectl") == str(expected)


def test_path_still_wins_when_it_has_kubectl(tmp_path, monkeypatch):
    """Searching is a fallback, not an override of the user's environment."""
    monkeypatch.setattr(binaries.shutil, "which", lambda _: "/usr/local/bin/kubectl")
    monkeypatch.setattr(
        binaries, "_candidate_dirs", lambda: [tmp_path / "never-consulted"]
    )

    assert binaries.resolve("kubectl") == "/usr/local/bin/kubectl"


def test_missing_kubectl_falls_back_to_the_bare_name(tmp_path, monkeypatch):
    """So the error stays the familiar "command not found"."""
    monkeypatch.setattr(binaries.shutil, "which", lambda _: None)
    monkeypatch.setattr(binaries, "_candidate_dirs", lambda: [tmp_path / "empty"])

    assert binaries.resolve("kubectl") == "kubectl"
    assert binaries.found("kubectl") is None


def test_found_reports_the_absolute_path(tmp_path, monkeypatch):
    expected = _make_executable(tmp_path / "bin")
    monkeypatch.setattr(binaries.shutil, "which", lambda _: None)
    monkeypatch.setattr(binaries, "_candidate_dirs", lambda: [tmp_path / "bin"])

    assert binaries.found("kubectl") == str(expected)


# ── explicit override ─────────────────────────────────────────────────────


def test_environment_override_wins(tmp_path, monkeypatch):
    override = _make_executable(tmp_path / "custom")
    monkeypatch.setenv("KUBEASTRA_KUBECTL_BINARY", str(override))
    monkeypatch.setattr(binaries.shutil, "which", lambda _: "/usr/bin/kubectl")

    assert binaries.resolve("kubectl") == str(override)


def test_bad_override_is_ignored_not_fatal(tmp_path, monkeypatch):
    """A stale override must not brick the app; fall through to discovery."""
    monkeypatch.setenv("KUBEASTRA_KUBECTL_BINARY", str(tmp_path / "gone"))
    monkeypatch.setattr(binaries.shutil, "which", lambda _: "/usr/bin/kubectl")

    assert binaries.resolve("kubectl") == "/usr/bin/kubectl"


def test_a_directory_is_not_accepted_as_the_binary(tmp_path, monkeypatch):
    """`is_file()`, not `exists()` — a directory is executable by os.access."""
    trap = tmp_path / "bin" / "kubectl"
    trap.mkdir(parents=True)
    monkeypatch.setattr(binaries.shutil, "which", lambda _: None)
    monkeypatch.setattr(binaries, "_candidate_dirs", lambda: [tmp_path / "bin"])

    assert binaries.resolve("kubectl") == "kubectl"


# ── PATH augmentation, for helm and kubectl plugins ───────────────────────


def test_augment_path_appends_missing_directories(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(binaries, "_candidate_dirs", lambda: [tools])

    assert binaries.augment_path() == [str(tools)]
    assert os.environ["PATH"] == f"/usr/bin:/bin:{tools}"


def test_augment_path_appends_never_prepends(tmp_path, monkeypatch):
    """A kubectl the user put first in PATH stays first."""
    tools = tmp_path / "tools"
    tools.mkdir()
    monkeypatch.setenv("PATH", f"/my/preferred:{tools}")
    monkeypatch.setattr(binaries, "_candidate_dirs", lambda: [tools])

    assert binaries.augment_path() == []
    assert os.environ["PATH"].startswith("/my/preferred")


def test_augment_path_skips_directories_that_do_not_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(binaries, "_candidate_dirs", lambda: [tmp_path / "nope"])

    assert binaries.augment_path() == []
    assert os.environ["PATH"] == "/usr/bin"
