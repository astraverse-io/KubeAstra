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

# Captured before the autouse fixture stubs it out, so the tests that
# exercise shell discovery itself can still reach the real thing.
real_login_shell_path = binaries.login_shell_path


@pytest.fixture(autouse=True)
def clear_cache(monkeypatch):
    # Never spawn the developer's real login shell: it makes results depend
    # on whoever is running the suite, and costs half a second each time.
    # Tests that care about shell discovery override this in their body.
    monkeypatch.setattr(binaries, "login_shell_path", lambda: None)
    monkeypatch.setattr(binaries, "_cloud_plugin_dirs", lambda: [])
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


# ── login-shell PATH discovery ────────────────────────────────────────────
#
# Known locations cannot fix credential plugins: a GKE/EKS/AKS kubeconfig
# names one under `exec`, kubectl resolves it through PATH itself, and it
# installs wherever its SDK went — `~/Downloads/google-cloud-sdk/bin` is a
# real example no hardcoded list would contain. So ask the user's shell.


class _Result:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def _shell_returning(path_value: str, *, noise: str = ""):
    marker = binaries._MARKER

    def fake_run(cmd, **kwargs):
        return _Result(f"{noise}{marker}{path_value}{marker}")

    return fake_run


def test_login_shell_path_is_extracted_from_between_markers(monkeypatch):
    """Real profiles print banners and version-manager chatter."""
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.delenv(binaries._DISABLE_ENV, raising=False)
    monkeypatch.setattr(binaries, "_executable", lambda _: True)
    monkeypatch.setattr(
        binaries.subprocess, "run",
        _shell_returning("/opt/sdk/bin:/usr/bin", noise="nvm: loaded\n"),
    )

    assert real_login_shell_path() == "/opt/sdk/bin:/usr/bin"


def test_login_shell_timeout_is_not_fatal(monkeypatch):
    """A shell that hangs must not stop the app from starting."""
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.delenv(binaries._DISABLE_ENV, raising=False)
    monkeypatch.setattr(binaries, "_executable", lambda _: True)

    def hang(cmd, **kwargs):
        raise binaries.subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(binaries.subprocess, "run", hang)
    assert real_login_shell_path() is None


def test_login_shell_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv(binaries._DISABLE_ENV, "1")

    def explode(cmd, **kwargs):  # pragma: no cover — must not be reached
        raise AssertionError("shell should not have been queried")

    monkeypatch.setattr(binaries.subprocess, "run", explode)
    assert real_login_shell_path() is None


def test_shell_path_reaches_a_plugin_no_list_would_guess(tmp_path, monkeypatch):
    """The end-to-end case: PATH extended so kubectl can find its exec plugin."""
    odd_location = tmp_path / "Downloads" / "google-cloud-sdk" / "bin"
    _make_executable(odd_location, "gke-gcloud-auth-plugin")

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(binaries, "login_shell_path", lambda: f"/usr/bin:{odd_location}")
    monkeypatch.setattr(binaries, "_candidate_dirs", lambda: [])
    monkeypatch.setattr(binaries, "_cloud_plugin_dirs", lambda: [])

    added = binaries.augment_path()

    assert str(odd_location) in added
    assert "/usr/bin" not in added, "already present; must not be duplicated"
    assert binaries.shutil.which("gke-gcloud-auth-plugin") is not None


def test_augment_path_survives_a_shell_that_says_nothing(tmp_path, monkeypatch):
    """Known locations still apply when shell discovery fails."""
    tools = tmp_path / "tools"
    tools.mkdir()
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(binaries, "login_shell_path", lambda: None)
    monkeypatch.setattr(binaries, "_candidate_dirs", lambda: [tools])
    monkeypatch.setattr(binaries, "_cloud_plugin_dirs", lambda: [])

    assert binaries.augment_path() == [str(tools)]


def test_missing_auth_plugins_reports_only_absent_ones(monkeypatch):
    present = {"gke-gcloud-auth-plugin"}
    monkeypatch.setattr(
        binaries.shutil, "which",
        lambda name: "/somewhere/" + name if name in present else None,
    )

    missing = binaries.missing_auth_plugins()
    assert "gke-gcloud-auth-plugin" not in missing
    assert "aws-iam-authenticator" in missing
