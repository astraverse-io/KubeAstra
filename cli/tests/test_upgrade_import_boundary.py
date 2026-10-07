"""Phase 3: the keyless CLI upgrade path must not drag in the server or an LLM.

Guards D1/D2: ``kubeastra.upgrade`` is the keyless plan path (CLI + GitHub Action),
so it must import nothing under ``ui/``, no backend HTTP client, no typer app, and
no LLM client. See PILOTS_PLAN.md §Phase 3.
"""
from pathlib import Path

_UPGRADE = Path(__file__).resolve().parents[1] / "src" / "kubeastra" / "upgrade.py"

# Heavy / server / LLM deps the keyless path must never import.
_FORBIDDEN = (
    "import ui",
    "from ui",
    "httpx",
    "typer",
    "fastapi",
    "anthropic",
    "openai",
    "langchain",
    "import db",
    "from db",
    "from .client",
    "from .config",
)


def test_keyless_upgrade_path_has_no_heavy_imports():
    offenders = []
    for line in _UPGRADE.read_text().splitlines():
        s = line.strip().lower()
        if s.startswith("import ") or s.startswith("from "):
            for bad in _FORBIDDEN:
                if bad in s:
                    offenders.append(line)
    assert not offenders, "keyless upgrade path pulled in a heavy/server dep:\n" + "\n".join(offenders)
