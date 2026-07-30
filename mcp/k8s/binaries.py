"""Locate the CLI tools KubeAstra shells out to, without trusting `PATH`.

A GUI application does not inherit the shell's environment. Double-clicking
the app in Finder starts it from launchd with roughly
`/usr/bin:/bin:/usr/sbin:/sbin`, and none of the places `kubectl` is actually
installed are on that list:

    Docker Desktop   /Applications/Docker.app/Contents/Resources/bin
    Rancher Desktop  ~/.rd/bin
    Homebrew         /opt/homebrew/bin  (arm64)  ·  /usr/local/bin (x86)

So every kubectl-backed tool failed with "kubectl not found" for anyone who
launched the app the normal way, while working perfectly from a terminal.
That difference is also why it was hard to catch: `open path/to.app` from a
shell *propagates the caller's environment*, so the obvious way to test a
"GUI launch" quietly handed the app a full developer PATH.

Two mechanisms, deliberately:

  * `resolve()` finds a specific tool and returns an absolute path. The
    runners use it, so command execution never depends on PATH at all.
  * `augment_path()` appends directories to `PATH`, which fixes everything
    *else* that shells out — helm, kubectl's own plugins, and the cloud
    credential plugins a GKE/EKS/AKS kubeconfig names under `exec`. kubectl
    resolves those by walking PATH itself, so we never see the call and
    cannot route it through `resolve()`.

Append, never prepend: a user who has deliberately put a particular kubectl
earlier in PATH keeps it.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Checked before PATH and before any search. Lets a user point at an exact
# binary when their layout is unusual, and lets tests avoid the real one.
_ENV_OVERRIDE = "KUBEASTRA_{tool}_BINARY"


def _candidate_dirs() -> List[Path]:
    """Directories worth searching, most specific first.

    Ordered by how likely the install is to be the one the user means, not
    alphabetically: a dedicated Kubernetes distribution beats a package
    manager, which beats the system directories.
    """
    home = Path.home()
    system = platform.system()

    if system == "Windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        return [
            Path(program_files) / "Docker" / "Docker" / "resources" / "bin",
            home / ".rd" / "bin",
            home / ".docker" / "bin",
            Path(program_files) / "Kubernetes" / "Minikube",
        ]

    dirs = [
        home / ".rd" / "bin",            # Rancher Desktop
        home / ".docker" / "bin",        # Docker Desktop (newer layouts)
        home / ".local" / "bin",
        home / "bin",
        Path("/opt/homebrew/bin"),       # Homebrew on Apple Silicon
        Path("/usr/local/bin"),          # Homebrew on Intel; curl-based installs
        Path("/opt/local/bin"),          # MacPorts
        Path("/usr/bin"),
        Path("/bin"),
    ]
    if system == "Darwin":
        # Docker Desktop ships kubectl inside its own bundle and symlinks it
        # into /usr/local/bin — but only if the user let it, and that symlink
        # is the first thing to go stale after an uninstall.
        dirs.insert(0, Path("/Applications/Docker.app/Contents/Resources/bin"))
    else:
        dirs.append(Path("/snap/bin"))   # Linux snap packages
    return dirs


def _executable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


_cache: Dict[str, str] = {}


def resolve(tool: str, *, use_cache: bool = True) -> str:
    """Absolute path to `tool`, or the bare name if it cannot be found.

    Falling back to the bare name is deliberate. If the tool genuinely is not
    installed, the resulting error should be the familiar "kubectl: command
    not found" rather than something invented here — and on the off chance
    the process environment can resolve it when we could not, it still works.
    """
    if use_cache and tool in _cache:
        return _cache[tool]

    found = _search(tool)
    if use_cache:
        _cache[tool] = found
    return found


def _search(tool: str) -> str:
    override = os.environ.get(_ENV_OVERRIDE.format(tool=tool.upper()))
    if override:
        if _executable(Path(override)):
            logger.info("%s: using %s (from environment)", tool, override)
            return override
        logger.warning(
            "%s: %s is set to %s, which is not an executable file — ignoring",
            tool, _ENV_OVERRIDE.format(tool=tool.upper()), override,
        )

    on_path = shutil.which(tool)
    if on_path:
        return on_path

    names = [tool, f"{tool}.exe"] if platform.system() == "Windows" else [tool]
    for directory in _candidate_dirs():
        for name in names:
            candidate = directory / name
            if _executable(candidate):
                logger.info(
                    "%s: not on PATH, found at %s — a GUI launch does not "
                    "inherit the shell environment",
                    tool, candidate,
                )
                return str(candidate)

    logger.warning(
        "%s: not found on PATH or in any known install location; commands "
        "using it will fail until it is installed",
        tool,
    )
    return tool


def kubectl() -> str:
    return resolve("kubectl")


def helm() -> str:
    return resolve("helm")


def found(tool: str) -> Optional[str]:
    """The resolved path, or None when it fell back to the bare name.

    Lets health checks report "no kubectl" honestly instead of printing a
    path that does not exist.
    """
    path = resolve(tool)
    return path if os.path.sep in path else None


# ── the user's real PATH ──────────────────────────────────────────────────
#
# Searching known locations fixes the tools we can name. It cannot fix the
# ones kubectl invokes on our behalf: a kubeconfig for GKE, EKS or AKS names
# a credential plugin under `users[].user.exec`, and kubectl resolves that
# name by walking PATH itself. We never see the call.
#
# Those plugins install wherever their SDK went — one real example is
# `~/Downloads/google-cloud-sdk/bin`, which no list of standard locations
# would ever contain. So instead of guessing, ask the user's login shell what
# its PATH is and merge that in. This is the same approach VS Code takes for
# the same problem, and it fixes kubectl, helm, every auth plugin and any
# custom install at once.

_SHELL_TIMEOUT_SECONDS = 5
_MARKER = "__KUBEASTRA_PATH__"
_DISABLE_ENV = "KUBEASTRA_NO_SHELL_PATH"


def login_shell_path() -> Optional[str]:
    """The PATH the user's login shell produces, or None.

    Runs the shell with `-i -l`. Interactive matters: on zsh, `PATH` is set in
    `.zshrc` far more often than in `.zprofile`, and a login-only shell never
    reads it. Output is bracketed by a marker because a real profile prints
    banners, version-manager noise and occasionally warnings.

    Returns None on any failure. A shell that hangs, errors, or is missing is
    a normal condition here, not something to propagate — discovery falling
    back to known locations is strictly better than failing to start.
    """
    if platform.system() == "Windows":
        return None
    if os.environ.get(_DISABLE_ENV):
        logger.info("login-shell PATH discovery disabled by %s", _DISABLE_ENV)
        return None

    shell = os.environ.get("SHELL") or "/bin/zsh"
    if not _executable(Path(shell)):
        logger.debug("login shell %s is not executable", shell)
        return None

    script = f'printf "{_MARKER}%s{_MARKER}" "$PATH"'
    # Guard against a profile that launches this app again.
    env = {**os.environ, _DISABLE_ENV: "1"}

    for args in (["-i", "-l", "-c", script], ["-l", "-c", script]):
        try:
            result = subprocess.run(
                [shell, *args],
                capture_output=True,
                text=True,
                timeout=_SHELL_TIMEOUT_SECONDS,
                check=False,
                env=env,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            logger.warning("login shell %s did not respond within %ds",
                           shell, _SHELL_TIMEOUT_SECONDS)
            continue
        except Exception as error:
            logger.warning("could not query login shell %s: %s", shell, error)
            continue

        parts = (result.stdout or "").split(_MARKER)
        if len(parts) >= 3 and parts[1].strip():
            return parts[1].strip()

    return None


def _cloud_plugin_dirs() -> List[Path]:
    """Standard SDK locations, as a fallback when the shell tells us nothing.

    Covers a default install of each cloud SDK. A non-default install is what
    `login_shell_path()` is for.
    """
    home = Path.home()
    return [
        home / "google-cloud-sdk" / "bin",
        Path("/opt/homebrew/share/google-cloud-sdk/bin"),
        Path("/usr/local/share/google-cloud-sdk/bin"),
        Path("/usr/lib/google-cloud-sdk/bin"),
        home / ".azure" / "kubelogin",
        home / ".krew" / "bin",           # kubectl plugin manager
    ]


# Credential plugins a kubeconfig can name. Reported by health checks so a
# cloud cluster failing to authenticate says *why*.
AUTH_PLUGINS = (
    "gke-gcloud-auth-plugin",
    "aws-iam-authenticator",
    "aws",
    "kubelogin",
)


def missing_auth_plugins() -> List[str]:
    """Which known credential plugins cannot be resolved right now."""
    return [name for name in AUTH_PLUGINS if shutil.which(name) is None]


def augment_path() -> List[str]:
    """Extend `PATH` so shelled-out tools and auth plugins resolve.

    Returns the directories added. Sources, in order of trust:

      1. the user's login shell — their actual environment
      2. known tool locations (`_candidate_dirs`)
      3. standard cloud SDK locations

    Appended, never prepended: a binary the user deliberately put earlier in
    PATH keeps winning.
    """
    current = os.environ.get("PATH", "")
    existing = {p for p in current.split(os.pathsep) if p}

    added: List[str] = []

    shell_path = login_shell_path()
    if shell_path:
        for entry in shell_path.split(os.pathsep):
            if entry and entry not in existing:
                existing.add(entry)
                added.append(entry)
        logger.info("login shell contributed %d PATH entries", len(added))

    for directory in [*_candidate_dirs(), *_cloud_plugin_dirs()]:
        text = str(directory)
        if text in existing:
            continue
        try:
            if not directory.is_dir():
                continue
        except OSError:
            continue
        existing.add(text)
        added.append(text)

    if added:
        os.environ["PATH"] = os.pathsep.join(
            [current, *added] if current else added
        )
        logger.info("PATH extended with %d director(ies)", len(added))
    return added


def reset_cache() -> None:
    """Forget resolved paths. For tests, and after `augment_path()`."""
    _cache.clear()
