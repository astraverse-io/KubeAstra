"""Nothing on the startup path may call a unix-only os function.

The first Windows sidecar built cleanly and then died before serving anything:

    AttributeError: module 'os' has no attribute 'geteuid'
      from k8s.kubectl_runner import kubectl, ...

`os.geteuid` and `os.getegid` do not exist on Windows. kubectl_runner called
them while logging the audit-log location — at import time, so the whole
backend went down — and the except handler that was supposed to report the
problem called them again, raising while reporting.

routers/cluster.py had both shapes: line 87 guarded with hasattr, line 43 did
not. The unguarded one would have been the next crash, the first time anyone
connected a cluster.

These tests run on every platform. On unix they cannot observe the failure
directly, so they assert the guard is present rather than the behaviour —
the behaviour only differs on the platform CI cannot run this suite on.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND = REPO_ROOT / "ui" / "backend"
MCP = REPO_ROOT / "mcp"
for path in (BACKEND, MCP):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# Attributes of `os` that simply are not there on Windows.
UNIX_ONLY = {"geteuid", "getegid", "getuid", "getgid", "getpwuid", "setuid", "fork"}

# Modules that the frozen backend imports on the way up. A bare call in any of
# these takes the whole app down before it can report why.
STARTUP_MODULES = [
    MCP / "k8s" / "kubectl_runner.py",
    BACKEND / "routers" / "cluster.py",
    BACKEND / "desktop_paths.py",
    BACKEND / "desktop_security.py",
    BACKEND / "main.py",
]


def _unguarded_unix_calls(source: str) -> list[str]:
    """`os.geteuid()` reached without a `hasattr(os, ...)` guard nearby.

    Deliberately crude: it treats any hasattr/try in the enclosing function as
    protection. The point is to catch a bare call added by someone who never
    ran the code on Windows, not to prove reachability.
    """
    tree = ast.parse(source)
    found: list[str] = []

    def calls_in(node) -> list[str]:
        return [
            inner.func.attr
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and isinstance(inner.func.value, ast.Name)
            and inner.func.value.id == "os"
            and inner.func.attr in UNIX_ONLY
        ]

    # Only function bodies. Walking the Module as well would re-reach every
    # call nested inside a function, with no guard in scope — which reported
    # each one twice and marked the guarded ones as offenders.
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = ast.unparse(node)
        guarded = (
            "hasattr(os," in body.replace(" ", "")
            or "except AttributeError" in body
        )
        if guarded:
            continue
        for attr in calls_in(node):
            found.append(f"{node.name}: os.{attr}()")

    # Module-level statements run at import, which is where the crash was.
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for attr in calls_in(stmt):
            found.append(f"<module>: os.{attr}()")
    return found


@pytest.mark.parametrize("path", STARTUP_MODULES, ids=lambda p: p.name)
def test_no_unguarded_unix_only_os_calls(path: Path):
    if not path.exists():
        pytest.skip(f"{path.name} not present")

    offenders = sorted(set(_unguarded_unix_calls(path.read_text())))

    assert offenders == [], (
        f"{path.relative_to(REPO_ROOT)} calls unix-only os functions without a "
        f"hasattr guard: {offenders}. On Windows this raises AttributeError, "
        f"and in a frozen build that means the backend never starts."
    )


def test_the_process_identity_helper_survives_a_missing_geteuid():
    """The helper that replaced the bare calls must not itself assume unix."""
    from k8s.kubectl_runner import _process_identity

    assert "uid=" in _process_identity()

    import os as os_module

    saved = getattr(os_module, "geteuid", None)
    try:
        if saved is not None:
            delattr(os_module, "geteuid")
        assert _process_identity() == "uid=n/a gid=n/a"
    finally:
        if saved is not None:
            os_module.geteuid = saved


def test_the_kubeconfig_dir_resolves_without_geteuid():
    """Windows has no geteuid and needs none — GetTempPath is already
    per-user. This must return a path rather than raise."""
    import os as os_module

    from routers import cluster

    saved = getattr(os_module, "geteuid", None)
    try:
        if saved is not None:
            delattr(os_module, "geteuid")
        result = cluster._kubeconfig_dir_path()
        assert result.name == "kubeastra-kubeconfigs"
    finally:
        if saved is not None:
            os_module.geteuid = saved
