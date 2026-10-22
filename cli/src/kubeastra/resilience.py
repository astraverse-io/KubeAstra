"""Keyless ``kubeastra resilience`` logic — the Resilience Pilot on the terminal / in CI.

Imports only the pure core (``mcp/services/resilience``), PyYAML and the
standard library: **no backend, no API key, no typer/httpx/config**, so it runs
in CI, in the GitHub Action, and is unit-testable in isolation. The Typer
command in ``cli.py`` just calls :func:`run_assess` and prints the result.

Two modes:
- ``PATH`` → static scan of rendered manifests (``helm template`` /
  ``kustomize build`` output or raw YAML). No cluster.
- ``--live`` → read the objects from the cluster with ``kubectl`` (default
  context, or ``--kubeconfig``). Unlike ``kubeastra upgrade``, a failed kubectl
  call is an error, not an empty cluster: "no workloads found" would otherwise
  pass a CI gate for the wrong reason.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

OUTPUTS = ("text", "json", "sarif")
SEVERITIES = ("low", "medium", "high")
CONFIG_PATH = Path(".kubeastra") / "resilience.yaml"
GROUP_ORDER = ("drain", "scaling", "rollout", "spread", "probes", "shutdown", "wiring", "hygiene")
_SARIF_LEVEL = {"high": "error", "medium": "warning", "low": "note"}

RunKubectl = Callable[[list[str]], Any]


class ResilienceCoreUnavailable(RuntimeError):
    """The pure resilience core (mcp/services/resilience) could not be located."""


class ResilienceUsageError(ValueError):
    """Bad input: nothing to scan, a bad config, kubectl failing in --live mode."""


def load_resilience_core():
    """Locate and import the pure resilience core — same order as
    :func:`kubeastra.upgrade.load_upgrade_core`: the copy bundled into the
    installed package, then an importable ``services.resilience``, then a
    sibling ``mcp/`` walking up from this file (repo checkout / the Action)."""
    try:
        from ._vendor.services import resilience as core

        return core
    except ImportError:
        pass
    try:
        import services.resilience as core  # type: ignore

        return core
    except ImportError:
        pass
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "mcp"
        if (candidate / "services" / "resilience" / "__init__.py").exists():
            sys.path.insert(0, str(candidate))
            try:
                import services.resilience as core  # type: ignore

                return core
            except ImportError:
                break
    raise ResilienceCoreUnavailable(
        "the KubeAstra resilience core (mcp/services/resilience) was not found — reinstall "
        "with `pip install --upgrade kubeastra`, or run from a repo checkout."
    )


# ── inputs ────────────────────────────────────────────────────────────────────

def load_config(config_path: Optional[str], cwd: Optional[Path] = None) -> Optional[dict]:
    """An explicit ``--config`` must exist; otherwise ``.kubeastra/resilience.yaml``
    in the working directory is used when present."""
    if config_path:
        p = Path(config_path)
        if not p.is_file():
            raise ResilienceUsageError(f"config file not found: {config_path}")
    else:
        p = (cwd or Path.cwd()) / CONFIG_PATH
        if not p.is_file():
            return None
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ResilienceUsageError(f"{p}: not valid YAML ({exc})") from exc
    if not isinstance(data, dict):
        raise ResilienceUsageError(f"{p}: expected a mapping with 'disable' and/or 'severity'")
    return data


def _kubectl(kubeconfig: Optional[str]) -> RunKubectl:
    def run(args: list[str]) -> Any:
        cmd = ["kubectl"] + (["--kubeconfig", kubeconfig] if kubeconfig else []) + args
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except FileNotFoundError as exc:
            raise ResilienceUsageError("kubectl not found on PATH (needed for --live)") from exc
        except subprocess.TimeoutExpired as exc:
            raise ResilienceUsageError(f"`{' '.join(cmd)}` timed out") from exc
        if proc.returncode != 0:
            raise ResilienceUsageError(f"`{' '.join(cmd)}` failed: {(proc.stderr or proc.stdout).strip()}")
        try:
            return json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ResilienceUsageError(f"`{' '.join(cmd)}` did not return JSON") from exc

    return run


def collect(core, path: Optional[str], *, live: bool, kubeconfig: Optional[str] = None,
            run_kubectl: Optional[RunKubectl] = None) -> tuple[list, Any, str]:
    """``(items, facts, source_mode)`` for :func:`core.assess`."""
    if live == bool(path):
        raise ResilienceUsageError("give either a PATH of rendered manifests or --live, not both/neither")
    if live:
        run = run_kubectl or _kubectl(kubeconfig)
        objects = run(["get", core.LIVE_KINDS, "--all-namespaces", "-o", "json"])
        nodes = run(["get", "nodes", "-o", "json"])
        return [objects], core.facts_from_nodes(nodes), "live"
    if not Path(path).exists():
        raise ResilienceUsageError(f"path not found: {path}")
    docs = core.scan_path(path)
    if not any(isinstance(d.doc, dict) and d.doc.get("kind") for d in docs):
        raise ResilienceUsageError(
            f"no Kubernetes manifests found under {path} (render a chart first: "
            "`helm template … > out/`)"
        )
    return docs, None, "static"


# ── --fix ─────────────────────────────────────────────────────────────────────

_GENERATED = ("patches", "new", "kustomization.yaml", "README.md")


def write_fixes(files: dict[str, str], fix_dir: str, marker: str) -> Path:
    """Write :func:`render_fixes` output under ``fix_dir``.

    Refuses a non-empty directory it didn't create (``--fix .`` must never
    overwrite a repo's README). A directory from an earlier ``--fix`` run is
    refreshed: only the files this command generates are replaced."""
    d = Path(fix_dir)
    if d.exists() and not d.is_dir():
        raise ResilienceUsageError(f"--fix {fix_dir}: not a directory")
    if d.exists() and any(d.iterdir()):
        if not (d / marker).is_file():
            raise ResilienceUsageError(
                f"--fix {fix_dir}: directory is not empty and wasn't written by --fix; use a new or empty directory"
            )
        for name in _GENERATED:
            p = d / name
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
    for rel, content in files.items():
        out = d / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content)
    return d


def fix_summary(report) -> dict[str, int]:
    counts = {"patch": 0, "new_object": 0, "advisory": 0}
    for f in report.findings:
        if f.fix is None or f.suppressed:
            continue
        counts["patch" if f.fix.kind in ("field", "block") else f.fix.kind] += 1
    return counts


# ── the command ───────────────────────────────────────────────────────────────

def gate(report, *, fail_under: Optional[int], fail_on: Optional[str]) -> list[str]:
    """Reasons the CI gate fails (empty = pass). Only scored findings count."""
    reasons = []
    if fail_under is not None and report.score is not None and report.score < fail_under:
        reasons.append(f"score {report.score} is below --fail-under {fail_under}")
    if fail_on:
        floor = SEVERITIES.index(fail_on)
        hits = [f for f in report.findings if f.scored and SEVERITIES.index(f.severity) >= floor]
        if hits:
            reasons.append(f"{len(hits)} finding(s) at or above --fail-on {fail_on}")
    return reasons


def run_assess(
    path: Optional[str] = None,
    *,
    live: bool = False,
    kubeconfig: Optional[str] = None,
    output: str = "text",
    fail_under: Optional[int] = None,
    fail_on: Optional[str] = None,
    config_path: Optional[str] = None,
    fix_dir: Optional[str] = None,
    core: Any = None,
    run_kubectl: Optional[RunKubectl] = None,
    cwd: Optional[Path] = None,
) -> tuple[int, str]:
    """Assess and render. Returns ``(exit_code, text)``: 1 when a gate is set
    and fails, else 0. Raises :class:`ResilienceUsageError` for bad input.
    ``fix_dir`` also writes the fixes there; it never changes the exit code."""
    if output not in OUTPUTS:
        raise ResilienceUsageError(f"--output must be one of {', '.join(OUTPUTS)}")
    if fail_on is not None and fail_on not in SEVERITIES:
        raise ResilienceUsageError(f"--fail-on must be one of {', '.join(SEVERITIES)}")
    if fail_under is not None and not 0 <= fail_under <= 100:
        raise ResilienceUsageError("--fail-under must be between 0 and 100")
    core = core or load_resilience_core()
    config = load_config(config_path, cwd)
    items, facts, mode = collect(core, path, live=live, kubeconfig=kubeconfig, run_kubectl=run_kubectl)
    try:
        report = core.assess(items, facts=facts, source_mode=mode, config=config)
    except ValueError as exc:  # a config naming an unknown rule / severity
        raise ResilienceUsageError(f"resilience config: {exc}") from exc
    reasons = gate(report, fail_under=fail_under, fail_on=fail_on)
    gated = fail_under is not None or fail_on is not None
    text = render(output, report, core, reasons=reasons, gated=gated, source=path or "live cluster", cwd=cwd)
    if output == "text":
        counts = fix_summary(report)
        made = counts["patch"] + counts["new_object"]
        if fix_dir:
            write_fixes(core.render_fixes(report), fix_dir, core.FIX_MARKER)
            text += (f"\n\nFixes written to {fix_dir}: {counts['patch']} patch{'' if counts['patch'] == 1 else 'es'}, "
                     f"{_n(counts['new_object'], 'new object')}, {counts['advisory']} that need you "
                     f"(see {Path(fix_dir) / 'README.md'})")
        elif made or counts["advisory"]:
            text += (f"\n\nFixes: {made} can be generated, {counts['advisory']} need you — "
                     "rerun with --fix DIR to write them")
    elif fix_dir:
        write_fixes(core.render_fixes(report), fix_dir, core.FIX_MARKER)
    return (1 if reasons else 0), text


# ── rendering ─────────────────────────────────────────────────────────────────

def render(output: str, report, core, *, reasons: list[str], gated: bool, source: str, cwd: Optional[Path] = None) -> str:
    if output == "json":
        data = report.to_dict()
        data["gate"] = {"enabled": gated, "passed": not reasons, "reasons": reasons}
        return json.dumps(data, indent=2)
    if output == "sarif":
        return json.dumps(_sarif(report, core, cwd), indent=2)
    return _text(report, reasons=reasons, gated=gated, source=source)


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _obj(r) -> str:
    return f"{r.kind} {r.namespace + '/' if r.namespace else ''}{r.name}"


def _at(r) -> str:
    return f"  ({Path(r.file).name}:{r.line})" if r.file else ""


def _text(report, *, reasons: list[str], gated: bool, source: str) -> str:
    scored = [f for f in report.findings if f.scored]
    advisory = [f for f in report.findings if f.advisory and not f.suppressed]
    suppressed = [f for f in report.findings if f.suppressed]
    score = "n/a" if report.score is None else f"{report.score}/100"
    lines = [
        f"KubeAstra Resilience Pilot — {report.source_mode} scan of {source}",
        f"Resilience score: {score} ({report.grade})   {_n(len(report.workloads), 'workload')}, {_n(len(scored), 'finding')}",
    ]
    for group in GROUP_ORDER:
        hits = [f for f in scored if f.group == group]
        if not hits:
            continue
        lines.append(f"\n{group.capitalize()} ({len(hits)})")
        for f in sorted(hits, key=lambda f: -SEVERITIES.index(f.severity)):
            lines.append(f"  {f.rule} {f.severity:<6} {' + '.join(_obj(r) for r in f.objects)}{_at(f.objects[0])}")
            lines.append(f"         {f.message}")
    if report.workloads:
        lines.append("\nWorkloads")
        for w in sorted(report.workloads, key=lambda w: (w.score, w.ref.name)):
            lines.append(f"  {w.score:>3} {w.grade}  {_obj(w.ref):<45} {' '.join(w.rules)}")
    if advisory:
        lines.append(f"\nAdvisory, not scored ({len(advisory)})")
        lines.extend(f"  {f.rule} {_obj(f.objects[0])}: {f.message}" for f in advisory)
    if suppressed:
        lines.append(f"\nSuppressed ({len(suppressed)}): " + ", ".join(f"{f.rule} {_obj(f.objects[0])}" for f in suppressed))
    if report.notes:
        lines.append("\nNotes:")
        lines.extend(f"  - {n}" for n in report.notes)
    if gated:
        lines.append("\nGate: " + ("FAIL — " + "; ".join(reasons) if reasons else "pass"))
    return "\n".join(lines)


def _uri(file: str, cwd: Optional[Path]) -> str:
    p = Path(file)
    try:
        return p.resolve().relative_to((cwd or Path.cwd()).resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def _sarif(report, core, cwd: Optional[Path]) -> dict:
    cat = core.load_rules()
    rules = [
        {
            "id": rid,
            "name": "".join(w.capitalize() for w in meta["title"].replace("/", " ").replace(":", " ").split()),
            "shortDescription": {"text": meta["title"]},
            "defaultConfiguration": {"level": _SARIF_LEVEL[meta["severity"]]},
            "properties": {"group": meta["group"]},
        }
        for rid, meta in sorted(cat["rules"].items())
    ]
    results = []
    for f in report.findings:
        first = f.objects[0]
        if first.file:
            loc = {"physicalLocation": {"artifactLocation": {"uri": _uri(first.file, cwd)}, "region": {"startLine": max(first.line, 1)}}}
        else:
            loc = {"logicalLocations": [{"name": _obj(first), "kind": "resource"}]}
        result = {
            "ruleId": f.rule,
            "level": "note" if f.advisory else _SARIF_LEVEL[f.severity],
            "message": {"text": f"{_obj(first)}: {f.message}"},
            "locations": [loc],
        }
        if f.suppressed:
            result["suppressions"] = [{"kind": "inSource", "justification": "resilience.kubeastra.io/ignore annotation"}]
        results.append(result)
    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [
            {
                "tool": {"driver": {"name": "kubeastra-resilience", "informationUri": "https://kubeastra.io", "rules": rules}},
                "results": results,
            }
        ],
    }
