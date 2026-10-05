"""Phase 1: the Upgrade Pilot data maps load and validate.

The API-deprecation map is the GUARANTEED core, so it is schema-checked here:
every entry has the required fields and an explicit `replacement` (possibly
null). See PILOTS_PLAN.md §Phase 1.
"""
import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import pytest  # noqa: E402

from services.upgrade import load_maps, minor_tuple, validate_api_deprecations  # noqa: E402
from services.upgrade.maps import _DATA_DIR, _load_yaml  # noqa: E402


def test_shipped_api_deprecations_is_valid():
    raw = _load_yaml(_DATA_DIR / "api_deprecations.yaml")
    problems = validate_api_deprecations(raw)
    assert problems == [], "api_deprecations.yaml invalid:\n" + "\n".join(problems)


def test_every_entry_sets_replacement_key_explicitly():
    raw = _load_yaml(_DATA_DIR / "api_deprecations.yaml")
    for e in raw["deprecations"]:
        assert "replacement" in e, f"{e.get('kind')} missing explicit replacement key"


def test_load_maps_strict_parses_known_removal():
    maps = load_maps(strict=True)
    ing = [
        d
        for d in maps["deprecations"]
        if d.kind == "Ingress" and d.version == "v1beta1" and d.group == "networking.k8s.io"
    ]
    assert ing, "expected the networking.k8s.io/v1beta1 Ingress removal"
    assert ing[0].removed_in == "1.22"
    assert ing[0].replacement == "networking.k8s.io/v1"
    assert ing[0].api_version == "networking.k8s.io/v1beta1"


def test_provider_and_operator_maps_load():
    maps = load_maps()
    assert "eks" in (maps["provider_eol"].get("providers") or {})
    assert maps["operator_compat"].get("operators")


def test_minor_tuple_parsing():
    assert minor_tuple("1.31") == (1, 31)
    assert minor_tuple("v1.31.4") == (1, 31)
    with pytest.raises(ValueError):
        minor_tuple("latest")


def test_validator_flags_missing_replacement():
    bad = {"deprecations": [{"kind": "X", "group": "g", "version": "v1", "removed_in": "1.25"}]}
    problems = validate_api_deprecations(bad)
    assert any("replacement" in p for p in problems)


def test_validator_flags_bad_version():
    bad = {"deprecations": [{"kind": "X", "group": "g", "version": "v1", "removed_in": "soon", "replacement": None}]}
    assert any("removed_in" in p for p in validate_api_deprecations(bad))
