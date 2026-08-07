"""Every surface that states a version must state the same one.

This is a public repo about to be pointed at from marketing, and it had three
answers to "what version is this?": the Helm chart said 1.0.0, the CLI and the
MCP server said 0.1.0, and the only release was v0.1.0. A chart claiming 1.0.0
next to a 0.1.0 CLI reads as carelessness, and it is the kind of drift nobody
notices because each file is individually plausible.

Nothing here says *which* version is right. It only says they agree — so a
release bump has to touch all of them, which is the actual requirement.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _mcp_version() -> str:
    text = (REPO_ROOT / "mcp" / "version.py").read_text()
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def _cli_dunder_version() -> str:
    text = (REPO_ROOT / "cli" / "src" / "kubeastra" / "__init__.py").read_text()
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def _cli_pyproject_version() -> str:
    text = (REPO_ROOT / "cli" / "pyproject.toml").read_text()
    return re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)


def _chart_version() -> str:
    text = (REPO_ROOT / "helm" / "kubeastra" / "Chart.yaml").read_text()
    return re.search(r'^version:\s*"?([^"\s]+)"?', text, re.M).group(1)


def _tauri_version() -> str:
    """The desktop app's own version.

    This one is not cosmetic: Tauri's updater compares it against the release
    feed to decide whether an update exists. It said 1.0.0 while everything
    else said 0.2.0, so a shipped app would have considered itself newer than
    every release and never updated itself.
    """
    text = (REPO_ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text()
    return json.loads(text)["version"]


def _cargo_version() -> str:
    """The Rust crate version.

    Tauri bundles using tauri.conf.json, so this one does not name the DMG —
    which is exactly why it drifted: nothing visible breaks. It still shows up
    in build output and crash reports, where a version that disagrees with the
    app sends whoever is reading it to the wrong commit.
    """
    text = (REPO_ROOT / "desktop" / "src-tauri" / "Cargo.toml").read_text()
    return re.search(r'^version = "([^"]+)"', text, re.M).group(1)


def _chart_app_version() -> str:
    text = (REPO_ROOT / "helm" / "kubeastra" / "Chart.yaml").read_text()
    return re.search(r'^appVersion:\s*"?([^"\s]+)"?', text, re.M).group(1)


SOURCES = {
    "mcp/version.py": _mcp_version,
    "cli/src/kubeastra/__init__.py": _cli_dunder_version,
    "cli/pyproject.toml": _cli_pyproject_version,
    "helm/kubeastra/Chart.yaml (version)": _chart_version,
    "helm/kubeastra/Chart.yaml (appVersion)": _chart_app_version,
    "desktop/src-tauri/tauri.conf.json": _tauri_version,
    "desktop/src-tauri/Cargo.toml": _cargo_version,
}


@pytest.mark.parametrize("name,read", sorted(SOURCES.items()))
def test_each_version_is_a_plain_semver(name: str, read):
    """`helm package` and PyPI both reject anything else, and a version that
    fails at publish time fails after the tag is already public."""
    assert SEMVER.match(read()), f"{name} is not a bare X.Y.Z version"


def test_every_declared_version_agrees():
    found = {name: read() for name, read in SOURCES.items()}

    assert len(set(found.values())) == 1, (
        "these disagree about the project version, so at least one is wrong:\n"
        + "\n".join(f"  {name}: {value}" for name, value in sorted(found.items()))
        + "\n\nBump them together, in the same commit as the release tag."
    )
