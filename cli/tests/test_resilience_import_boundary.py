"""Resilience Pilot Phase 1: the keyless CLI path must not drag in the server,
the backend client or an LLM — same guard as test_upgrade_import_boundary.py."""
import ast
from pathlib import Path

_MODULE = Path(__file__).resolve().parents[1] / "src" / "kubeastra" / "resilience.py"

# Top-level modules the keyless path may import (relative `._vendor` / `services`
# imports of the pure core are the point of the module and are allowed).
_ALLOWED = {"__future__", "json", "pathlib", "subprocess", "sys", "typing", "yaml", "services"}


def test_keyless_resilience_path_imports_only_stdlib_yaml_and_the_core():
    offenders = []
    for node in ast.walk(ast.parse(_MODULE.read_text())):
        if isinstance(node, ast.Import):
            mods = [(a.name, 0) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [(node.module or "", node.level)]
        else:
            continue
        for mod, level in mods:
            if level:  # relative: only the bundled core
                if not (mod or "").startswith("_vendor"):
                    offenders.append(f"line {node.lineno}: from {'.' * level}{mod}")
            elif mod.split(".")[0] not in _ALLOWED:
                offenders.append(f"line {node.lineno}: {mod}")
    assert not offenders, "keyless resilience path pulled in a heavy/server dep:\n" + "\n".join(offenders)
