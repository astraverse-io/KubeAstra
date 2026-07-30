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
  * `augment_path()` appends the directories we found to `PATH`, which fixes
    everything *else* that shells out — helm, and kubectl's own plugins,
    which kubectl locates by scanning PATH itself.

Append, never prepend: a user who has deliberately put a particular kubectl
earlier in PATH keeps it.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
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


def augment_path() -> List[str]:
    """Append known tool directories to `PATH`. Returns the ones added.

    For everything that shells out without going through `resolve()` —
    notably helm, and kubectl plugins, which kubectl discovers by walking
    PATH itself.
    """
    current = os.environ.get("PATH", "")
    existing = {p for p in current.split(os.pathsep) if p}

    added = [
        str(d) for d in _candidate_dirs()
        if str(d) not in existing and d.is_dir()
    ]
    if added:
        os.environ["PATH"] = os.pathsep.join(
            [current, *added] if current else added
        )
        logger.info("PATH extended with %d tool director(ies)", len(added))
    return added


def reset_cache() -> None:
    """Forget resolved paths. For tests, and after `augment_path()`."""
    _cache.clear()
