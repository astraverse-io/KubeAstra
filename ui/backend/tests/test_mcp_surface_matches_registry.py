"""What the MCP server exposes, against what the registry says it should.

`docs/ARCHITECTURE_DIAGRAM.md` calls `tool_registry.py` the single source of
truth for tools. It was not. `mcp_server/tools.py` held a second, hand-written
list, and the two drifted: the registry carried 51 tools on the `mcp` surface
while the server returned 48. `get_investigation_details`, `get_recent_alerts`
and `prom_query` were reachable from chat and invisible to every IDE.

Nothing failed. The server announced it on every start — "Registered 48 tools"
— and `README.md`, `docs/ARCHITECTURE_DIAGRAM.md`, kubeastra.io and
astraverse.dev all went on saying 51.

These tests are about the *seam*, not the tools: that the two halves agree,
and that anything listed can also be called. A tool the IDE shows and the
dispatcher rejects is worse than one it never showed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[3] / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import tool_registry  # noqa: E402
from mcp_server.tools import get_tools_definitions  # noqa: E402


def _exposed() -> list[str]:
    return [tool.name for tool in get_tools_definitions()]


def _registry_mcp_tools() -> set[str]:
    return {
        name
        for name, definition in tool_registry.TOOLS.items()
        if "mcp" in definition.surfaces
    }


def test_every_registry_mcp_tool_is_exposed():
    missing = sorted(_registry_mcp_tools() - set(_exposed()))

    assert not missing, (
        "These carry the `mcp` surface but no IDE can see them:\n  "
        + "\n  ".join(missing)
    )


def test_nothing_is_exposed_that_the_registry_does_not_have():
    """The other direction: a tool listed but absent from the registry would
    dispatch into `_dispatch_via_registry` and come back "Unknown tool"."""
    extra = sorted(set(_exposed()) - _registry_mcp_tools())

    assert not extra, "Exposed over MCP but not in the registry:\n  " + "\n  ".join(extra)


def test_no_tool_is_listed_twice():
    """The generated half must skip what the hand-written half already declares.

    Duplicates are not cosmetic: a repeated name in `list_tools` is ambiguous
    to the client, and which definition wins is undefined.
    """
    exposed = _exposed()
    duplicates = sorted({name for name in exposed if exposed.count(name) > 1})

    assert not duplicates, "Declared more than once: " + ", ".join(duplicates)


def test_chat_only_tools_stay_off_the_mcp_surface():
    """Guards the generated half against over-reaching.

    Generating from the registry is only safe while it filters on the surface.
    Drop that filter and the 16 chat-only tools — which include the write
    operations — appear in every IDE.
    """
    chat_only = {
        name
        for name, definition in tool_registry.TOOLS.items()
        if "mcp" not in definition.surfaces
    }

    assert not (chat_only & set(_exposed()))


@pytest.mark.parametrize(
    "name", ["get_investigation_details", "get_recent_alerts", "prom_query"]
)
def test_the_three_recovered_tools_are_callable(name):
    """Listing is half of it; these must survive an actual dispatch.

    Asserted on the shape rather than the content — `prom_query` with no
    Prometheus configured and `get_investigation_details` with an unknown id
    both return an error dict, which is a real answer. A raised exception, or
    the registry refusing the `mcp` surface, is not.
    """
    from mcp_server.tools import _dispatch_via_registry

    params = {
        "get_investigation_details": {"investigation_id": "does-not-exist"},
        "get_recent_alerts": {"limit": 1},
        "prom_query": {"query": "up"},
    }[name]

    result = _dispatch_via_registry(name, params)

    assert isinstance(result, dict)
    assert result.get("error") != f"Unknown tool: {name}"


def test_an_unknown_name_is_reported_not_raised():
    """The dispatcher is the `else` branch of `call_tool`, so it sees typos."""
    from mcp_server.tools import _dispatch_via_registry

    assert _dispatch_via_registry("no_such_tool", {}) == {
        "error": "Unknown tool: no_such_tool"
    }


def test_registry_backed_definitions_carry_a_usable_schema():
    """A Tool with no inputSchema is listed and then uncallable in practice."""
    for tool in get_tools_definitions():
        assert isinstance(tool.inputSchema, dict), tool.name
        assert tool.inputSchema.get("type") == "object", tool.name
        assert tool.description, f"{tool.name} has no description"
