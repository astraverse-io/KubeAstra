"""Resilience Pilot Phase 0: the core must stay pure, like the Upgrade core.

No ``ui/``, DB, LLM, web framework or subprocess — that's what lets the server,
the keyless CLI and the GitHub Action share one engine. The shared manifest
reader is checked too, since both cores (and the vendored CLI copy) import it.
Imports are read with ``ast`` so docstring examples don't count.
"""
import ast
import json
import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

FILES = sorted((MCP_DIR / "services" / "resilience").glob("*.py")) + [MCP_DIR / "services" / "manifests.py"]

# Top-level modules the core may import. Anything else is a new dependency and
# needs a deliberate decision (and the CLI's dependency list).
ALLOWED = {"__future__", "copy", "dataclasses", "functools", "math", "pathlib", "re", "typing", "yaml"}


def _imports(py: Path) -> list[tuple[int, str, int]]:
    """(line, module, level) for every import statement in ``py``."""
    out = []
    for node in ast.walk(ast.parse(py.read_text())):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name, 0) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            out.append((node.lineno, node.module or "", node.level))
    return out


def test_core_imports_only_stdlib_and_yaml():
    offenders = [
        f"{py.name}:{line}: {mod}"
        for py in FILES
        for line, mod, level in _imports(py)
        if level == 0 and mod.split(".")[0] not in ALLOWED
    ]
    assert not offenders, "pure core imported something outside the allow-list:\n" + "\n".join(offenders)


def test_core_imports_its_siblings_relatively():
    """The CLI vendors the core as ``kubeastra._vendor.services.resilience``; an
    absolute ``services.`` import would work in the repo and break on PyPI."""
    offenders = [f"{py.name}:{line}: {mod}" for py in FILES for line, mod, level in _imports(py) if level == 0 and mod.startswith("services")]
    assert not offenders, offenders


def test_assess_smoke_round_trips_json():
    from services.resilience import assess

    report = assess([])
    json.dumps(report.to_dict())
