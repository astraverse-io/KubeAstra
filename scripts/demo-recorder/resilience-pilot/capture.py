"""Run the Resilience Pilot demo commands for real and record their output verbatim.

The GIF replays what this writes to timeline.json and nothing else, so every
line of output in it is something `kubeastra` actually printed. Run it with the
Python of a venv where kubeastra was installed from PyPI:

    python capture.py <workdir> <timeline.json>

<workdir> must contain rendered/checkout.yaml (copied from fixtures/).
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
    "kubeastra resilience rendered/ --fail-on high",
    'echo "exit code: $?"',
    "kubeastra resilience rendered/ --fix fixes/ | tail -n 1",
    "cat fixes/patches/poddisruptionbudget-shop-checkout.yaml",
]


def _require_installed_package() -> str:
    """Refuse to record a checkout: the GIF is evidence the PyPI package works."""
    import kubeastra.resilience as res  # noqa: PLC0415 — the venv's kubeastra

    core = res.load_resilience_core().__name__
    if core != "kubeastra._vendor.services.resilience":
        sys.exit(
            f"capture: kubeastra loaded its resilience core from {core!r}, not the copy "
            "bundled in the installed package. Record from a venv with "
            "`pip install kubeastra`, outside a repo checkout, with PYTHONPATH unset."
        )
    return core


def main() -> None:
    workdir, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    if not (workdir / "rendered" / "checkout.yaml").is_file():
        sys.exit(f"capture: {workdir}/rendered/checkout.yaml not found")
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

    scan, fixed = timeline[1], timeline[4]
    if scan["rc"] != 1 or "RES001 high" not in scan["out"] or "RES003 high" not in scan["out"]:
        sys.exit(f"capture: expected RES001 + RES003 and exit 1, got rc={scan['rc']}:\n{scan['out']}")
    if "minAvailable: 1" not in fixed["out"]:
        sys.exit(f"capture: expected the PDB patch to unblock the drain, got:\n{fixed['out']}")

    version = timeline[0]["out"]  # `kubeastra --version`, e.g. "kubeastra 0.3.0"
    out.write_text(json.dumps({"recorded_with": version, "core": core, "steps": timeline}, indent=1) + "\n")
    print(f"capture: {len(timeline)} steps, {version} at {exe} -> {out}")


if __name__ == "__main__":
    main()
