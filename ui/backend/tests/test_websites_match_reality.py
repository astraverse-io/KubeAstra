"""The public websites must not contradict the code.

`test_docs_match_reality.py` keeps the in-repo docs honest by comparing them
against `tool_registry.TOOLS`. It cannot see kubeastra.io or astraverse.dev,
which live in separate repositories — and both drifted to "51 tools" while the
registry said 52, in seven places on kubeastra.io alone including the meta
description and the OpenGraph and Twitter cards. Nothing failed, because
nothing was looking.

These tests hit the network, so they are **off by default**: a unit suite that
needs DNS is a unit suite that fails on a plane, and a CI job that fails when
GitHub Pages is slow teaches people to ignore red. Set

    CHECK_LIVE_WEBSITES=true

to run them. `check-websites.yml` does that on a schedule and after each
release, which is when drift actually matters.

Following the same live/offline split as the evals suite (`EVAL_LIVE_LLM`).
"""

from __future__ import annotations

import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MCP_DIR = REPO_ROOT / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

SITES = {
    "kubeastra.io": "https://kubeastra.io/",
    "astraverse.dev": "https://astraverse.dev/",
}

# The org was renamed from `kubeastra` to `astraverse-io`. Links to the old
# name still resolve, but only through a redirect GitHub drops the moment
# somebody registers the abandoned org name.
DEAD_ORG = "github.com/kubeastra/kubeastra"

LIVE = os.environ.get("CHECK_LIVE_WEBSITES", "").lower() in ("true", "1", "yes")

pytestmark = pytest.mark.skipif(
    not LIVE, reason="set CHECK_LIVE_WEBSITES=true to check the public websites"
)


def _real_tool_count() -> int:
    import tool_registry

    return len(tool_registry.TOOLS)


def _fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "kubeastra-doc-check"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.read().decode("utf-8", "ignore")
    except urllib.error.URLError as error:
        # A site being unreachable is a real problem, but not the one this file
        # is about — say which so nobody debugs the tool count for an hour.
        pytest.fail(f"{url} could not be fetched: {error}")


@pytest.fixture(scope="module")
def pages() -> dict[str, str]:
    return {name: _fetch(url) for name, url in SITES.items()}


@pytest.mark.parametrize("site", sorted(SITES))
def test_the_site_is_actually_serving(site: str, pages: dict[str, str]):
    """A guard on the guard: every assertion below passes vacuously against an
    empty response."""
    assert len(pages[site]) > 2000, f"{site} returned suspiciously little HTML"


@pytest.mark.parametrize("site", sorted(SITES))
def test_no_stale_tool_count(site: str, pages: dict[str, str]):
    """Any two- or three-digit number followed by "tools" must be the real one.

    Deliberately not "does 52 appear" — that passes while a stale 51 sits three
    paragraphs down, which is exactly the state both sites were in.
    """
    expected = _real_tool_count()
    # Up to two words may sit between the number and "tools". A tighter
    # pattern misses "51 Investigation Tools", which is one of the headings
    # that was actually wrong — and a guard that catches six of seven is worse
    # than none, because it reads as proof the page is clean.
    claims = {int(n) for n in re.findall(
        r"\b(\d{2,3})\s+(?:[A-Za-z][A-Za-z-]*\s+){0,2}tools?\b", pages[site], re.I)}
    wrong = claims - {expected}

    assert not wrong, (
        f"{site} claims {sorted(wrong)} tools; the registry has {expected}. "
        f"The count appears in several places including the meta description "
        f"and the OpenGraph/Twitter cards — fix all of them."
    )


@pytest.mark.parametrize("site", sorted(SITES))
def test_no_links_to_the_abandoned_org(site: str, pages: dict[str, str]):
    assert DEAD_ORG not in pages[site], (
        f"{site} links to {DEAD_ORG}. That resolves only through GitHub's "
        f"org-rename redirect, which stops working if anyone registers the "
        f"old name. Use github.com/astraverse-io/KubeAstra."
    )


def test_the_product_site_tells_people_how_to_install(pages: dict[str, str]):
    """The site shipped for months with no install path at all — no 'brew', no
    'download', no '.dmg' — while the signed app was already on Homebrew."""
    html = pages["kubeastra.io"]

    assert "brew install --cask kubeastra" in html, "no Homebrew command on the product site"
    assert "releases/latest" in html, "no link to download the build"


def test_the_advertised_install_command_matches_the_published_cask():
    """The site could say `brew install kubeastra` (a formula) while the tap
    ships a cask, and every visitor would get 'No available formula'."""
    cask = _fetch(
        "https://raw.githubusercontent.com/astraverse-io/homebrew-tap/main/Casks/kubeastra.rb"
    )
    html = _fetch(SITES["kubeastra.io"])

    assert 'cask "kubeastra"' in cask
    assert "--cask kubeastra" in html, (
        "the tap ships a cask but the site does not say --cask"
    )


def test_the_advertised_download_actually_exists():
    """`releases/latest` is only useful if the newest release carries a DMG.
    A desktop release that failed to attach one would leave the button on a
    page with nothing to download."""
    request = urllib.request.Request(
        "https://api.github.com/repos/astraverse-io/KubeAstra/releases",
        headers={"User-Agent": "kubeastra-doc-check",
                 "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        import json

        releases = json.load(response)

    desktop = [r for r in releases
               if r["tag_name"].startswith("desktop-v") and not r["draft"]]
    assert desktop, "no published desktop release for the download link to reach"

    assets = [a["name"] for a in desktop[0]["assets"]]
    assert any(a.endswith(".dmg") for a in assets), (
        f"newest desktop release {desktop[0]['tag_name']} has no DMG: {assets}"
    )
