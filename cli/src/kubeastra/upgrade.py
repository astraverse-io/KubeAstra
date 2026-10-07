"""Keyless ``kubeastra upgrade`` logic — the Upgrade Pilot on the terminal / in CI.

Imports only the pure planner core (``mcp/services/upgrade``) + the standard
library: **no backend, no API key, no typer/httpx/config**. So it runs in CI,
in a GitHub Action, and is unit-testable in isolation. The thin Typer command in
``cli.py`` just calls :func:`run_plan` and prints the result.

Two modes, both keyless for the *plan*:
- ``--manifests DIR`` → static scan of rendered manifests (no cluster).
- otherwise → a live scan of the cluster via ``kubectl`` (default context, or
  ``--kubeconfig``).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional


class UpgradeCoreUnavailable(RuntimeError):
    """The pure planner core (mcp/services/upgrade) could not be located."""


def load_upgrade_core():
    """Locate and import the pure upgrade core.

    Works when ``services.upgrade`` is already importable, or by finding a sibling
    ``mcp/`` directory walking up from this file (a repo checkout / the Action).
    """
    try:
        import services.upgrade as core  # type: ignore

        return core
    except ImportError:
        pass
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "mcp"
        if (candidate / "services" / "upgrade" / "__init__.py").exists():
            sys.path.insert(0, str(candidate))
            try:
                import services.upgrade as core  # type: ignore

                return core
            except ImportError:
                break
    raise UpgradeCoreUnavailable(
        "the KubeAstra upgrade core (mcp/services/upgrade) was not found — run from "
        "a repo checkout, or install the kubeastra core package."
    )


def _live_run_json(kubeconfig: Optional[str]):
    def run_json(args: list[str]) -> dict:
        cmd = ["kubectl"] + (["--kubeconfig", kubeconfig] if kubeconfig else []) + args
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except Exception:
            return {}
        if proc.returncode != 0:
            return {}
        try:
            return json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return {}

    return run_json


def build_snapshot(core, *, manifests: Optional[str], kubeconfig: Optional[str]):
    if manifests:
        return core.scan_manifests(manifests)
    return core.scan_cluster(_live_run_json(kubeconfig), core.load_maps()["deprecations"])


def run_plan(
    target: str,
    *,
    manifests: Optional[str] = None,
    kubeconfig: Optional[str] = None,
    output: str = "text",
    fail_on_blocking: bool = True,
    core: Any = None,
) -> tuple[int, str]:
    """Produce the readiness report + plan and render it.

    Returns ``(exit_code, text)``: exit_code is 1 when blocking APIs are found and
    ``fail_on_blocking`` is set (the CI gate), else 0.
    """
    core = core or load_upgrade_core()
    maps = core.load_maps()
    report = core.assess(build_snapshot(core, manifests=manifests, kubeconfig=kubeconfig), target, maps)
    migration = core.plan(report, maps)
    text = render(output, report, migration)
    exit_code = 1 if (fail_on_blocking and report.blocking) else 0
    return exit_code, text


def render(output: str, report, plan) -> str:
    if output == "json":
        return json.dumps({"report": report.to_dict(), "plan": plan.to_dict()}, indent=2)
    if output == "sarif":
        return json.dumps(_sarif(report), indent=2)
    return _text(report, plan)


def _where(b) -> str:
    return f"{b.namespace}/{b.name}" if b.namespace else (b.name or b.gvk)


def _text(report, plan) -> str:
    lines = [
        f"KubeAstra Upgrade Pilot — target {report.target} "
        f"(current {report.current or 'unknown'}, provider {report.provider})"
    ]
    if not report.blocking:
        lines.append("No blocking API removals found for the target. OK")
    else:
        lines.append(f"\nBlocking ({len(report.blocking)}):")
        for b in report.blocking:
            repl = b.replacement or "NO in-place replacement (manual rework)"
            lines.append(f"  - {b.gvk} ({_where(b)}) -> {repl}")
    if report.operators:
        lines.append("\nOperators (advisory):")
        for o in report.operators:
            lines.append(f"  - {o.name}: installed {o.installed or '?'} / min {o.min_required or '?'} [{o.action}]")
    if report.notes:
        lines.append("\nNotes:")
        lines.extend(f"  - {n}" for n in report.notes)
    lines.append(f"\nPlan ({len(plan.steps)} steps):")
    for s in plan.steps:
        lines.append(f"  {s.order}. [{s.kind}/{s.risk}] {s.title}")
    return "\n".join(lines)


def _sarif(report) -> dict:
    results = []
    for b in report.blocking:
        repl = b.replacement or "no in-place replacement"
        results.append(
            {
                "ruleId": "deprecated-api-removed",
                "level": "error",
                "message": {"text": f"{b.gvk} is removed in Kubernetes {report.target}; migrate to {repl}."},
                "locations": [{"physicalLocation": {"artifactLocation": {"uri": _where(b)}}}],
            }
        )
    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "kubeastra-upgrade",
                        "informationUri": "https://kubeastra.io",
                        "rules": [
                            {
                                "id": "deprecated-api-removed",
                                "name": "DeprecatedApiRemoved",
                                "shortDescription": {
                                    "text": "An in-use API is removed at the target Kubernetes version."
                                },
                            }
                        ],
                    }
                },
                "results": results,
            }
        ],
    }
