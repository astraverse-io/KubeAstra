#!/usr/bin/env python3
"""Guard the desktop dependency footprint against silent growth.

The desktop app ships a bundled Python runtime, so every megabyte of
dependency is a megabyte of installer. The risk is not a deliberate addition —
it is a small package quietly pulling in a large transitive one (adding torch
back would be +770MB, and nothing in review would obviously say so).

A fixed "under 400MB" target does not catch that: a change can double the size
and still pass. This compares against a recorded baseline instead and fails on
relative growth.

Usage:
    python desktop/check_size.py --site-packages <path>   # CI
    python desktop/check_size.py --update                 # accept new size
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASELINE_PATH = HERE / "size-baseline.json"
MIB = 1024 * 1024


def directory_size_mib(path: Path) -> float:
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            # Files can vanish mid-walk (pip cleanup, editors); skipping one
            # is far better than failing the build on a race.
            continue
    return total / MIB


def largest_packages(path: Path, count: int = 10) -> list[tuple[str, float]]:
    sizes = []
    for child in path.iterdir():
        if child.is_dir() and not child.name.endswith(".dist-info"):
            sizes.append((child.name, directory_size_mib(child)))
    sizes.sort(key=lambda item: item[1], reverse=True)
    return sizes[:count]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-packages", type=Path, help="site-packages directory to measure")
    parser.add_argument("--update", action="store_true", help="write the measured size as the new baseline")
    args = parser.parse_args(argv)

    baseline = json.loads(BASELINE_PATH.read_text())
    recorded = float(baseline["site_packages_mib"])
    tolerance = float(baseline.get("tolerance_percent", 10))

    if args.site_packages is None:
        print("--site-packages is required", file=sys.stderr)
        return 2
    if not args.site_packages.is_dir():
        print(f"not a directory: {args.site_packages}", file=sys.stderr)
        return 2

    measured = directory_size_mib(args.site_packages)
    limit = recorded * (1 + tolerance / 100)
    delta_pct = ((measured - recorded) / recorded) * 100 if recorded else 0.0

    print(f"baseline : {recorded:.0f} MiB")
    print(f"measured : {measured:.0f} MiB  ({delta_pct:+.1f}%)")
    print(f"limit    : {limit:.0f} MiB  (baseline +{tolerance:.0f}%)")

    if args.update:
        baseline["site_packages_mib"] = round(measured)
        BASELINE_PATH.write_text(json.dumps(baseline, indent=2) + "\n")
        print(f"\nbaseline updated to {round(measured)} MiB")
        return 0

    print("\ntop packages:")
    for name, size in largest_packages(args.site_packages):
        print(f"  {size:7.1f} MiB  {name}")

    if measured > limit:
        print(
            f"\nFAIL: dependencies grew {delta_pct:+.1f}%, past the "
            f"{tolerance:.0f}% tolerance.\n"
            f"Check the list above for a newly-added or newly-inflated "
            f"package. If the growth is intended, run:\n"
            f"    python desktop/check_size.py --site-packages <path> --update\n"
            f"and say why in the commit message.",
            file=sys.stderr,
        )
        return 1

    print("\nOK — within tolerance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
