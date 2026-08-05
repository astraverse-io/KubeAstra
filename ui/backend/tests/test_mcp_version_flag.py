"""`--version` on both MCP entry points.

Added on top of the contributed change (#9) rather than inside it, so the
contributor's commit stays as they wrote it.

The flag is worth pinning because of *how* it is implemented. `argparse`'s
`action="version"` prints and calls `sys.exit(0)` from inside `parse_args`, so
the flag has to be handled before the server does any real work — one import
moved below it and `--version` starts building a server instead of answering.
Rebasing this change already required restoring two module-level imports that
an automatic merge had quietly dropped; a subprocess check is the only thing
that would have caught that, since the file still compiled.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[3] / "mcp"

ENTRY_POINTS = ["mcp_server.server", "http_mcp.http_server"]


def _run(module: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", module, "--version"],
        cwd=MCP_DIR,
        capture_output=True,
        text=True,
        timeout=60,
        env={"PYTHONPATH": str(MCP_DIR), "PATH": "/usr/bin:/bin"},
    )


@pytest.mark.parametrize("module", ENTRY_POINTS)
def test_version_flag_prints_and_exits_cleanly(module):
    result = _run(module)

    assert result.returncode == 0, (
        f"`python -m {module} --version` exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # argparse writes --version to stdout; the servers log to stderr, so a
    # caller parsing stdout gets the version and nothing else.
    assert "kubeastra-mcp" in result.stdout


@pytest.mark.parametrize("module", ENTRY_POINTS)
def test_version_flag_does_not_start_a_server(module):
    """The flag must short-circuit before the server binds or connects.

    "Starting ... MCP Server" on stderr means `parse_args` ran too late — the
    process did real work before printing a version string.
    """
    result = _run(module)

    assert "Starting" not in result.stderr, (
        f"{module} began starting up before handling --version:\n{result.stderr}"
    )


def test_both_entry_points_report_the_same_version():
    """One constant, two callers. Divergence here means someone hardcoded it."""
    reported = {module: _run(module).stdout.strip() for module in ENTRY_POINTS}

    assert len(set(reported.values())) == 1, reported


def test_the_reported_version_is_the_one_in_version_py():
    sys.path.insert(0, str(MCP_DIR))
    try:
        from version import VERSION_DISPLAY, __version__
    finally:
        sys.path.remove(str(MCP_DIR))

    assert __version__ in VERSION_DISPLAY
    assert _run("mcp_server.server").stdout.strip() == VERSION_DISPLAY
