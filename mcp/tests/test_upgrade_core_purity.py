"""Phase 0: the Upgrade Pilot core must stay pure.

A clean, dependency-free core (no ``ui/``, no DB, no LLM, no web framework) is
what lets the server, the keyless CLI, and the GitHub Action all share one
planner. This test fails loudly if a later edit drags a heavy dependency into
``mcp/services/upgrade``. See ``internal_docs/features/PILOTS_PLAN.md`` §Phase 0.
"""
import json
import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]  # .../mcp
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import pytest  # noqa: E402

PKG = MCP_DIR / "services" / "upgrade"

# Substrings that must never appear in an import line inside the pure core.
FORBIDDEN = (
    "fastapi",
    "pydantic",
    "sqlite3",
    "anthropic",
    "openai",
    "google.generativeai",
    "langchain",
    "import db",
    "from db",
    "import ui",
    "from ui",
    "requests",
    "httpx",
)


def _import_lines(path: Path) -> list[str]:
    out = []
    for raw in path.read_text().splitlines():
        s = raw.strip()
        if s.startswith("import ") or s.startswith("from "):
            out.append(s)
    return out


def test_core_has_no_heavy_imports():
    offenders = []
    for py in sorted(PKG.glob("*.py")):
        for line in _import_lines(py):
            low = line.lower()
            for bad in FORBIDDEN:
                if bad in low:
                    offenders.append(f"{py.name}: {line}  (matched {bad!r})")
    assert not offenders, "pure core imported something heavy:\n" + "\n".join(offenders)


def test_assess_and_plan_smoke():
    from services.upgrade import ClusterSnapshot, assess, plan

    snap = ClusterSnapshot(cluster_version="1.29", provider="eks")
    report = assess(snap, "1.31")
    assert report.current == "1.29"
    assert report.target == "1.31"
    assert report.provider == "eks"
    assert isinstance(report.blocking, list)

    p = plan(report)
    assert p.target == "1.31"
    assert isinstance(p.steps, list)

    # Everything round-trips as plain JSON — no custom types leak out.
    json.dumps(report.to_dict())
    json.dumps(p.to_dict())
    assert ClusterSnapshot.from_dict(snap.to_dict()).provider == "eks"


def test_static_mode_notes_the_operator_caveat():
    from services.upgrade import ClusterSnapshot, assess

    report = assess(ClusterSnapshot(source_mode="static"), "1.31")
    assert any("Static mode" in n for n in report.notes)


def test_assess_requires_a_target():
    from services.upgrade import ClusterSnapshot, assess

    with pytest.raises(ValueError):
        assess(ClusterSnapshot(), "")
