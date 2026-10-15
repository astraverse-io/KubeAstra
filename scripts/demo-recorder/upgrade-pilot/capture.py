"""Run the demo commands for real and record their output verbatim.

The GIF replays what this writes to timeline.json and nothing else, so every
line of output in it is something `kubeastra` actually printed. Run it with the
Python of a venv where kubeastra was installed from PyPI:

    python capture.py <workdir> <timeline.json>

<workdir> must contain rendered/payments.yaml (copied from fixtures/).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

STEPS = [
    "kubeastra --version",
    "grep -h '^apiVersion' rendered/payments.yaml",
    "kubeastra upgrade plan --target 1.31 --manifests rendered/",
    'echo "exit code: $?"',
]


def _require_installed_package() -> str:
    """Refuse to record a checkout: the GIF is evidence the PyPI package works."""
    import kubeastra.upgrade as up  # noqa: PLC0415 — the venv's kubeastra

    core = up.load_upgrade_core().__name__
    if core != "kubeastra._vendor.services.upgrade":
        sys.exit(
            f"capture: kubeastra loaded its upgrade core from {core!r}, not the copy "
            "bundled in the installed package. Record from a venv with "
            "`pip install kubeastra`, outside a repo checkout, with PYTHONPATH unset."
        )
    return core


def main() -> None:
    workdir, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    if not (workdir / "rendered" / "payments.yaml").is_file():
        sys.exit(f"capture: {workdir}/rendered/payments.yaml not found")
    core = _require_installed_package()

    # The venv's bin/ first, so `kubeastra` is the package being checked above.
    env = {**os.environ, "COLUMNS": "86", "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"}
    env.pop("PYTHONPATH", None)
    exe = shutil.which("kubeastra", path=env["PATH"])

    timeline, last_rc = [], 0
    for cmd in STEPS:
        # `$?` in a later step must be the previous step's real exit code.
        p = subprocess.run(
            ["bash", "-c", f"(exit {last_rc}); {cmd}"],
            cwd=workdir, env=env, capture_output=True, text=True,
        )
        timeline.append({"cmd": cmd, "out": (p.stdout + p.stderr).rstrip("\n"), "rc": p.returncode})
        last_rc = p.returncode

    plan = timeline[2]
    if plan["rc"] != 1 or "Blocking (3)" not in plan["out"]:
        sys.exit(f"capture: expected 3 blocking APIs and exit 1, got rc={plan['rc']}:\n{plan['out']}")

    version = timeline[0]["out"]  # `kubeastra --version`, e.g. "kubeastra 0.2.3"
    out.write_text(json.dumps({"recorded_with": version, "core": core, "steps": timeline}, indent=1) + "\n")
    print(f"capture: {len(timeline)} steps, {version} at {exe} -> {out}")


if __name__ == "__main__":
    main()
