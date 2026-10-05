"""build_native_tool_specs — registry → provider-neutral native tool schemas.

These specs feed every provider's generate_with_tools. The invariants that
matter: one well-formed object schema per react-enabled tool, the same tool set
the text harness sees, and honoring the allowed_tools scoping hook.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tool_registry import (  # noqa: E402
    build_native_tool_specs,
    tools_for_surface,
    valid_tool_names,
)


def _expected_react_tool_names() -> set[str]:
    return {t.name for t in tools_for_surface("react") if t.react_enabled}


def test_specs_cover_every_react_enabled_tool():
    specs = build_native_tool_specs()
    names = {s["name"] for s in specs}
    assert names == _expected_react_tool_names()
    # Sanity: the registry is non-trivial.
    assert len(specs) >= 10


def test_every_spec_is_a_valid_object_schema():
    for spec in build_native_tool_specs():
        assert spec["name"] in valid_tool_names("react")
        assert isinstance(spec["description"], str) and spec["description"]
        schema = spec["input_schema"]
        assert isinstance(schema, dict)
        assert schema.get("type") == "object"
        # Pydantic always emits a properties map (possibly empty).
        assert "properties" in schema


def test_allowed_tools_narrows_the_set():
    all_specs = build_native_tool_specs()
    pick = all_specs[0]["name"]
    scoped = build_native_tool_specs(allowed_tools={pick})
    assert [s["name"] for s in scoped] == [pick]


def test_allowed_tools_empty_yields_nothing():
    assert build_native_tool_specs(allowed_tools=set()) == []


def test_unknown_allowed_tool_is_ignored():
    scoped = build_native_tool_specs(allowed_tools={"does_not_exist"})
    assert scoped == []


def test_no_schema_uses_defs_or_refs():
    """Guard: a tool with a nested Pydantic model / Enum would emit $defs/$ref,
    which Gemini's parameters_json_schema (and strict OpenAI) can reject. All
    react tools are flat today; this fails loudly if someone adds a nested one
    without flattening the schema first."""
    for spec in build_native_tool_specs():
        blob = str(spec["input_schema"])
        assert "$defs" not in spec["input_schema"], f"{spec['name']} schema has $defs"
        assert "$ref" not in blob, f"{spec['name']} schema has $ref"
