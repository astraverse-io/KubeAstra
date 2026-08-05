"""Claims in the public docs, checked against the code that makes them true.

Documentation drifts silently. Nothing fails, nothing goes red, and the only
signal is a reader acting on a number that stopped being correct months ago.

The tool count is the clearest case: `tool_registry.TOOLS` grew to 51 while
`mcp/README.md` and `docs/ARCHITECTURE_DIAGRAM.md` went on saying 48 in five
places, and `README.md` said 51 in four others. Two numbers, both public, both
confidently stated. A reader had no way to tell which was current.

These tests are deliberately narrow. They cover claims that are *mechanically
checkable* — a count, a command name — and make no attempt to police prose.
Docs that are merely out of date in tone are a review problem; docs that state
a wrong number are a correctness problem, and that is what this file is for.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MCP_DIR = REPO_ROOT / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

# Every doc that quotes a tool count, and must therefore quote the real one.
DOCS_QUOTING_TOOL_COUNT = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "mcp" / "README.md",
    REPO_ROOT / "docs" / "ARCHITECTURE_DIAGRAM.md",
]

_COUNT_CLAIM = re.compile(r"\b(\d{2,3})\s+tools\b")


def _real_tool_count() -> int:
    import tool_registry

    return len(tool_registry.TOOLS)


@pytest.mark.parametrize("path", DOCS_QUOTING_TOOL_COUNT, ids=lambda p: p.name)
def test_documented_tool_count_matches_the_registry(path):
    expected = _real_tool_count()
    text = path.read_text(encoding="utf-8")

    wrong = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in _COUNT_CLAIM.finditer(line):
            if int(match.group(1)) != expected:
                wrong.append(f"{path.name}:{lineno}: {line.strip()}")

    assert not wrong, (
        f"tool_registry.TOOLS has {expected} entries. These say otherwise:\n  "
        + "\n  ".join(wrong)
    )


def test_every_cli_command_is_documented():
    """A command nobody documents is a command nobody finds.

    `kubeastra open` shipped and went unmentioned in both `cli/README.md` and
    the module docstring, which still announced "Six commands total" after the
    seventh landed.
    """
    cli_source = (REPO_ROOT / "cli" / "src" / "kubeastra" / "cli.py").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "cli" / "README.md").read_text(encoding="utf-8")

    # Typer takes the invoked name from the decorator when given one, and from
    # the function name otherwise — so `@config_app.command("set")` on
    # `def config_set` is typed as `kubeastra config set`, not `config_set`.
    # Sub-apps are mounted under their own prefix.
    pattern = re.compile(
        r"@(?P<app>app|config_app)\.command\(\s*(?:\"(?P<name>[\w-]+)\")?[^)]*\)\s*\ndef (?P<func>\w+)"
    )
    commands = []
    for match in pattern.finditer(cli_source):
        name = match.group("name") or match.group("func")
        prefix = "config " if match.group("app") == "config_app" else ""
        commands.append(f"{prefix}{name}")

    assert commands, "no commands found — has the CLI stopped using Typer?"

    undocumented = [c for c in commands if f"kubeastra {c}" not in readme]
    assert not undocumented, (
        "These commands exist but cli/README.md never shows them:\n  "
        + "\n  ".join(f"kubeastra {c}" for c in undocumented)
    )


# Number words are how prose counts things, and prose is where the drift hid:
# `demo/README.md` said "six intentionally-broken workloads" in two places and
# "These seven cover..." in a third, in the same file, after
# `07-secret-missing-job.yaml` landed. The manifests are the fact.
_NUMBER_WORDS = {
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_WORKLOAD_CLAIM = re.compile(
    r"\b(?P<count>" + "|".join(_NUMBER_WORDS) + r"|\d+)\b"
    r"(?P<gap>[\w,\- ]{0,30}?)"
    r"\b(?:broken|intentionally-broken)\b",
    re.IGNORECASE,
)

DOCS_QUOTING_WORKLOAD_COUNT = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "demo" / "README.md",
]


def _real_workload_count() -> int:
    manifests = (REPO_ROOT / "demo" / "broken-workloads").glob("*.yaml")
    # 00-namespace.yaml sets the stage; it is not one of the broken things.
    return sum(1 for m in manifests if not m.name.startswith("00-"))


@pytest.mark.parametrize(
    "path", DOCS_QUOTING_WORKLOAD_COUNT, ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_documented_demo_workload_count_matches_the_manifests(path):
    expected = _real_workload_count()
    text = path.read_text(encoding="utf-8")

    wrong = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in _WORKLOAD_CLAIM.finditer(line):
            raw = match.group("count").lower()
            claimed = _NUMBER_WORDS.get(raw, int(raw) if raw.isdigit() else None)
            if claimed is not None and claimed != expected:
                wrong.append(f"{path.name}:{lineno}: {line.strip()}")

    assert not wrong, (
        f"demo/broken-workloads/ holds {expected} broken workloads. "
        "These say otherwise:\n  " + "\n  ".join(wrong)
    )
