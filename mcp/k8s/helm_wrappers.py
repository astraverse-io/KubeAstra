"""Read-only Helm investigation wrappers.

Built on the read-only HelmRunner and the shared redaction helpers. v1 tools:
helm_available, list_helm_releases, get_helm_release. investigate_helm_release is
deferred to Phase 1.5 (see HELM_SUPPORT_PLAN.md).
"""
import difflib
import json
import logging
import re
from typing import Any, Dict, List, Optional

from k8s import redaction
from k8s.helm_runner import get_helm_runner, HelmError
from k8s.validators import validate_namespace, validate_resource_name

logger = logging.getLogger(__name__)

_HELM_VALUES_CAP = 16384       # bytes for `helm get values --all`
_HELM_MANIFEST_CAP = 32768     # bytes for `helm get manifest` / hooks
_HELM_NOTES_CAP = 8192         # bytes for `helm get notes`
_HELM_DIFF_CAP = 32768         # bytes for a revision diff
_INV_MAX_RESOURCES = 100       # cap resources parsed from a release manifest
_INV_MAX_WARNINGS = 10         # cap warning events surfaced
_INV_MAX_UNHEALTHY = 20        # cap unhealthy pods surfaced
_HEALTHY_POD_STATUSES = {"Running", "Succeeded", "Completed"}
_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Pod", "CronJob", "Job"}
_FAILED_REVISION_STATUSES = {"failed", "pending-upgrade", "pending-install", "pending-rollback"}
_DEFAULT_SECTIONS = ["status", "history", "values"]
_VALID_SECTIONS = ("status", "history", "values", "manifest", "hooks", "notes", "metadata")
# Sections for which a specific --revision N makes sense (history is inherently
# all-revisions, so it is the only section that ignores --revision).
_REVISIONED_SECTIONS = {"status", "values", "manifest", "hooks", "notes", "metadata"}

# `helm list` status filters -> flag.
_LIST_STATUS_FLAGS = {
    "failed": "--failed",
    "pending": "--pending",
    "deployed": "--deployed",
    "superseded": "--superseded",
    "uninstalling": "--uninstalling",
}


def _looks_unavailable(text: str) -> bool:
    low = (text or "").lower()
    return (
        "helm: command not found" in low
        or "helm: not found" in low
        or "helm binary not found" in low
        or "helm executable file not found" in low
        or "not found in $path" in low
    )


def _helm_target(runner) -> str:
    return "ssh_target" if getattr(runner, "ssh_runner", None) is not None else "backend"


def _helm_unavailable_fields(runner, error: str) -> Dict[str, Any]:
    target = _helm_target(runner)
    if target == "ssh_target":
        hint = (
            "Helm is not installed or reachable on the SSH target host. "
            "Install Helm on that target server, because Helm commands run there "
            "when the assistant is connected over SSH."
        )
    else:
        hint = (
            "Helm is not installed or reachable in the backend runtime. "
            "Install Helm in the backend image/container, because Helm commands "
            "run locally in the backend when no SSH target is active."
        )
    return {
        "available": False,
        "reason": "helm_unavailable",
        "target": target,
        "error": (error or "helm unavailable").strip(),
        "message": "Helm is not installed or reachable on the active execution target.",
        "remediation_hint": hint,
    }


def _helm_failure_fields(runner, error: str) -> Dict[str, Any]:
    target = _helm_target(runner)
    return {
        "available": False,
        "reason": "helm_check_failed",
        "target": target,
        "error": (error or "helm check failed").strip(),
        "message": "The assistant could not verify Helm on the active execution target.",
        "remediation_hint": (
            "Verify the active target is reachable and that Helm can run there. "
            "For SSH mode, check the SSH connection and remote shell environment. "
            "For backend mode, check the backend container runtime."
        ),
    }


def helm_available() -> Dict[str, Any]:
    """Detect whether helm is installed and reachable on the active target."""
    runner = get_helm_runner()
    try:
        result = runner.run(["version", "--short"])
    except HelmError as exc:
        error = str(exc)
        return (
            _helm_unavailable_fields(runner, error)
            if _looks_unavailable(error)
            else _helm_failure_fields(runner, error)
        )
    except ValueError as exc:  # forbidden command — should not happen for version
        return {"available": False, "error": str(exc)}
    if not result.success:
        error = result.stderr or "helm unavailable"
        return (
            _helm_unavailable_fields(runner, error)
            if _looks_unavailable(error)
            else _helm_failure_fields(runner, error)
        )
    return {"available": True, "version": result.stdout.strip()}


def list_helm_releases(
    namespace: Optional[str] = None,
    all_namespaces: bool = False,
    status_filter: Optional[str] = None,
) -> Dict[str, Any]:
    """List Helm releases in a namespace (or all namespaces when requested).

    ``status_filter`` (failed/pending/deployed/superseded/uninstalling) narrows
    the result to releases in that state; unknown values are ignored.
    """
    runner = get_helm_runner()
    args = ["list", "-o", "json"]
    ns: Optional[str] = None
    if all_namespaces:
        args.append("-A")
    else:
        ns = validate_namespace(namespace or "default")
    applied_filter: Optional[str] = None
    if status_filter:
        normalized = str(status_filter).strip().lower()
        flag = _LIST_STATUS_FLAGS.get(normalized)
        if flag:
            args.append(flag)
            applied_filter = normalized

    try:
        result = runner.run(args, namespace=ns)
    except HelmError as exc:
        error = str(exc)
        out = (
            _helm_unavailable_fields(runner, error)
            if _looks_unavailable(error)
            else _helm_failure_fields(runner, error)
        )
        out["releases"] = []
        return out

    if not result.success:
        unavailable = _looks_unavailable(result.stderr)
        if unavailable:
            out = _helm_unavailable_fields(runner, result.stderr or "helm list failed")
            out["releases"] = []
            return out
        return {
            "available": True,
            "error": (result.stderr or "helm list failed").strip(),
            "releases": [],
        }

    try:
        raw = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return {"available": True, "error": f"could not parse helm output: {exc}", "releases": []}

    releases = [
        {
            "name": r.get("name", ""),
            "namespace": r.get("namespace", ""),
            "revision": r.get("revision", ""),
            "status": r.get("status", ""),
            "chart": r.get("chart", ""),
            "app_version": r.get("app_version", ""),
            "updated": r.get("updated", ""),
        }
        for r in (raw if isinstance(raw, list) else [])
        if isinstance(r, dict)
    ]
    return {
        "available": True,
        "namespace": "*" if all_namespaces else ns,
        "status_filter": applied_filter,
        "release_count": len(releases),
        "releases": releases,
    }


def _validate_revision(revision) -> Optional[int]:
    """A revision must be a positive integer (not a bool, not a string flag)."""
    if revision is None:
        return None
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise ValueError("revision must be a positive integer")
    return revision


def get_helm_release(
    release: str,
    namespace: str,
    sections: Optional[List[str]] = None,
    revision: Optional[int] = None,
) -> Dict[str, Any]:
    """Read a named Helm release's status/history/values, plus optional sections.

    ``sections`` defaults to ["status", "history", "values"]; "manifest",
    "hooks", "notes", "metadata" are fetched only when explicitly requested.
    ``revision`` (a positive integer) reads a specific past revision for the
    revisioned sections (status/values/manifest/hooks/notes/metadata; history is
    always all-revisions) — the building block for read-only release diffing.
    Returns partial results: a failing
    section records its error and does not fail the whole call. Values,
    manifests, and hooks are redacted and size-capped.
    """
    namespace = validate_namespace(namespace)
    release = validate_resource_name(release)
    revision = _validate_revision(revision)
    requested = [s for s in (sections or _DEFAULT_SECTIONS) if s in _VALID_SECTIONS]
    if not requested:
        requested = list(_DEFAULT_SECTIONS)

    runner = get_helm_runner()
    out: Dict[str, Any] = {
        "release": release,
        "namespace": namespace,
        "revision": revision,
        "sections_requested": requested,
        "sections": {},
        "errors": {},
    }

    def _rev(section: str) -> List[str]:
        return ["--revision", str(revision)] if revision and section in _REVISIONED_SECTIONS else []

    def _run(args: List[str]):
        return runner.run(args, namespace=namespace)

    for section in requested:
        try:
            if section == "status":
                res = _run(["status", release, "-o", "json"] + _rev(section))
                out["sections"]["status"] = _parse_status(res.stdout) if res.success else None
            elif section == "history":
                res = _run(["history", release, "-o", "json"])
                out["sections"]["history"] = _parse_history(res.stdout) if res.success else None
            elif section == "values":
                res = _run(["get", "values", release, "--all", "-o", "yaml"] + _rev(section))
                out["sections"]["values"] = (
                    redaction.redact_value("values", res.stdout, _HELM_VALUES_CAP)
                    if res.success else None
                )
            elif section == "manifest":
                res = _run(["get", "manifest", release] + _rev(section))
                out["sections"]["manifest"] = (
                    redaction.redact_manifest(res.stdout, _HELM_MANIFEST_CAP)
                    if res.success else None
                )
            elif section == "hooks":
                # Rendered hook manifests — can include Secret hook resources.
                res = _run(["get", "hooks", release] + _rev(section))
                out["sections"]["hooks"] = (
                    redaction.redact_manifest(res.stdout, _HELM_MANIFEST_CAP)
                    if res.success else None
                )
            elif section == "notes":
                # NOTES.txt is prose — use the prose-aware (inline) redactor.
                res = _run(["get", "notes", release] + _rev(section))
                out["sections"]["notes"] = (
                    redaction.redact_prose(res.stdout, _HELM_NOTES_CAP)
                    if res.success else None
                )
            elif section == "metadata":
                res = _run(["get", "metadata", release, "-o", "json"] + _rev(section))
                out["sections"]["metadata"] = _parse_metadata(res.stdout) if res.success else None
            else:  # pragma: no cover - filtered above
                continue
            if not res.success:
                error = (res.stderr or "section failed").strip()
                out["errors"][section] = error
                if _looks_unavailable(error):
                    out.update(_helm_unavailable_fields(runner, error))
        except HelmError as exc:
            error = str(exc)
            out["errors"][section] = error
            if _looks_unavailable(error):
                out.update(_helm_unavailable_fields(runner, error))

    out["found"] = any(v is not None for v in out["sections"].values())
    return out


def diff_helm_revisions(
    release: str,
    namespace: str,
    from_revision: int,
    to_revision: int,
    section: str = "values",
) -> Dict[str, Any]:
    """Unified diff of two revisions of a release's values or rendered manifest.

    Read-only. Both sides are **redacted before diffing** (a changed secret shows
    no diff line — both render as ``***redacted***`` — so the diff never leaks
    secret values). The diff output is size-capped. ``section`` is ``values``
    (default) or ``manifest``.

    Because the diff is computed over redacted content, ``changed: false`` means
    "no non-secret changes" — a change limited to secret values would be hidden.
    The result carries ``redaction_may_hide_secret_only_changes: true`` so the
    caller does not overstate "no changes".
    """
    namespace = validate_namespace(namespace)
    release = validate_resource_name(release)
    frm = _validate_revision(from_revision)
    to = _validate_revision(to_revision)
    if frm is None or to is None:
        raise ValueError("from_revision and to_revision are required positive integers")
    section = (section or "values").lower()
    if section not in ("values", "manifest"):
        raise ValueError("section must be 'values' or 'manifest'")

    runner = get_helm_runner()

    def _fetch(rev: int):
        try:
            if section == "values":
                res = runner.run(
                    ["get", "values", release, "--all", "-o", "yaml", "--revision", str(rev)],
                    namespace=namespace,
                )
                redactor = lambda s: redaction.redact_value("values", s, _HELM_VALUES_CAP)
            else:
                res = runner.run(
                    ["get", "manifest", release, "--revision", str(rev)], namespace=namespace
                )
                redactor = lambda s: redaction.redact_manifest(s, _HELM_MANIFEST_CAP)
        except HelmError as exc:
            return None, str(exc)
        if not res.success:
            return None, (res.stderr or "fetch failed").strip()
        return redactor(res.stdout), None

    a_text, a_err = _fetch(frm)
    b_text, b_err = _fetch(to)

    errors: Dict[str, str] = {}
    if a_err:
        errors[f"revision_{frm}"] = a_err
    if b_err:
        errors[f"revision_{to}"] = b_err

    out: Dict[str, Any] = {
        "release": release,
        "namespace": namespace,
        "section": section,
        "from_revision": frm,
        "to_revision": to,
    }
    if a_text is None or b_text is None:
        out["diff"] = None
        out["errors"] = errors
        unavailable_error = next((err for err in errors.values() if _looks_unavailable(err)), None)
        if unavailable_error:
            out.update(_helm_unavailable_fields(runner, unavailable_error))
            out["errors"] = errors
        return out

    diff_lines = list(difflib.unified_diff(
        a_text.splitlines(), b_text.splitlines(),
        fromfile=f"revision {frm}", tofile=f"revision {to}", lineterm="",
    ))
    diff_text = "\n".join(diff_lines)
    truncated = False
    if len(diff_text) > _HELM_DIFF_CAP:
        diff_text = diff_text[:_HELM_DIFF_CAP] + "\n[... diff truncated ...]"
        truncated = True

    out.update({
        # `changed` reflects the diff of REDACTED content: a change confined to
        # secret values renders identically on both sides and will not appear, so
        # `changed: false` means "no non-secret changes", not "byte-identical".
        "changed": bool(diff_lines),
        "redaction_may_hide_secret_only_changes": True,
        "diff": diff_text,
        "truncated": truncated,
        "errors": errors or None,
    })
    return out


def _extract_manifest_resources(manifest: str, limit: int = _INV_MAX_RESOURCES) -> List[dict]:
    """Extract (kind, name) pairs from a rendered manifest (redaction-safe — kind
    and metadata.name are not secret). Line-based, no YAML dependency."""
    resources: List[dict] = []
    seen = set()
    for doc in re.split(r"(?m)^---\s*$", manifest or ""):
        kind = name = None
        in_meta = False
        for line in doc.splitlines():
            mk = re.match(r"^kind:\s*[\"']?([\w.\-]+)", line)
            if mk:
                kind = mk.group(1)
                continue
            if re.match(r"^metadata:\s*$", line):
                in_meta = True
                continue
            if in_meta:
                mn = re.match(r"^\s+name:\s*[\"']?([\w.\-]+)", line)
                if mn:
                    name = mn.group(1)
                    in_meta = False
                elif re.match(r"^\S", line):  # left the metadata block
                    in_meta = False
            if kind and name:
                break
        if kind and name and (kind, name) not in seen:
            seen.add((kind, name))
            resources.append({"kind": kind, "name": name})
        if len(resources) >= limit:
            break
    return resources


def _pod_belongs(pod_name: str, workload_names: set) -> bool:
    """Best-effort: a pod belongs to a release workload if its name matches the
    workload name or starts with ``<workload>-`` (Deployment->RS->Pod,
    StatefulSet-<ordinal>, DaemonSet-<hash>)."""
    return any(pod_name == w or pod_name.startswith(w + "-") for w in workload_names)


def _release_pod_health(namespace: str, workload_names: set) -> dict:
    """Pod health for pods owned by this release's workloads (best-effort).

    When the release has no extractable workloads, scoping is impossible: report
    ``scoped: false`` and do not let it drive the health verdict.
    """
    try:
        from k8s.wrappers import get_pods
        data = get_pods(namespace)
    except Exception as exc:  # kubectl failure must not crash the investigation
        return {"error": str(exc), "scoped": False}
    pods = data.get("pods", []) if isinstance(data, dict) else []
    scoped = bool(workload_names)
    matched, unhealthy = [], []
    for p in pods:
        if not isinstance(p, dict):
            continue
        name = p.get("name", "")
        if scoped and not _pod_belongs(name, workload_names):
            continue
        matched.append(p)
        status = str(p.get("status", ""))
        if status not in _HEALTHY_POD_STATUSES or p.get("ready") is False:
            unhealthy.append({
                "name": name, "status": status,
                "ready": p.get("ready"), "restarts": p.get("restarts"),
            })
    return {
        "scoped": scoped,
        "scope": "release_workloads" if scoped else "namespace_wide",
        "pod_count": len(matched),
        "unhealthy_count": len(unhealthy),
        "unhealthy": unhealthy[:_INV_MAX_UNHEALTHY],
    }


def _release_warnings(
    namespace: str, resource_keys: set, workload_names: set, limit: int = _INV_MAX_WARNINGS
) -> dict:
    """Warning events for this release's resources (best-effort).

    Keeps events whose involved object is a release resource, or a Pod owned by a
    release workload. When the release has no extractable resources, falls back to
    namespace-wide (``scoped: false``)."""
    try:
        from k8s.wrappers import get_events
        data = get_events(namespace, field_selector="type=Warning")
    except Exception as exc:
        return {"error": str(exc), "scoped": False}
    events = data.get("events", []) if isinstance(data, dict) else []
    scoped = bool(resource_keys or workload_names)
    warnings = []
    for e in events:
        if not isinstance(e, dict) or str(e.get("type", "")) != "Warning":
            continue
        obj = e.get("object", {}) if isinstance(e.get("object"), dict) else {}
        kind = e.get("kind") or obj.get("kind")
        name = e.get("name") or obj.get("name") or ""
        if scoped:
            belongs = (kind, name) in resource_keys or (kind == "Pod" and _pod_belongs(name, workload_names))
            if not belongs:
                continue
        warnings.append({
            "reason": e.get("reason", ""),
            "message": (e.get("message", "") or "")[:200],
            "count": e.get("count"),
            "kind": kind,
            "name": name,
            "last_timestamp": e.get("last_timestamp"),
        })
        if len(warnings) >= limit:
            break
    return {
        "scoped": scoped,
        "scope": "release_resources" if scoped else "namespace_wide",
        "count": len(warnings),
        "warnings": warnings,
    }


def investigate_helm_release(release: str, namespace: str) -> Dict[str, Any]:
    """Composite read-only investigation of a Helm release.

    Combines release status + recent history + the rendered resource list with
    live Kubernetes state (pod health and recent warning events) and a simple
    health assessment. All read-only; manifests are redacted before parsing.
    Resources are extracted from `helm get manifest` (reliable across Helm
    versions); per-step failures are recorded, not fatal.

    Pod health and warnings are scoped to this release's resources (best-effort,
    by workload-name matching) so unrelated noise in a shared namespace does not
    make the release look unhealthy; the ``scoped`` flag reports whether scoping
    was possible. ``release_healthy`` is driven by the CURRENT release status and
    release-scoped pod health only — older failed revisions are surfaced in
    ``prior_failed_revisions`` as context, not as health blockers.
    """
    namespace = validate_namespace(namespace)
    release = validate_resource_name(release)
    runner = get_helm_runner()
    out: Dict[str, Any] = {"release": release, "namespace": namespace, "errors": {}}

    # 1. Release status (if this fails, the release likely doesn't exist — stop).
    try:
        res = runner.run(["status", release, "-o", "json"], namespace=namespace)
        out["status"] = _parse_status(res.stdout) if res.success else None
        if not res.success:
            error = (res.stderr or "status failed").strip()
            out["errors"]["status"] = error
            if _looks_unavailable(error):
                out.update(_helm_unavailable_fields(runner, error))
    except HelmError as exc:
        error = str(exc)
        out["status"] = None
        out["errors"]["status"] = error
        if _looks_unavailable(error):
            out.update(_helm_unavailable_fields(runner, error))
    if out.get("status") is None:
        out["found"] = False
        return out

    # 2. Recent revisions (to spot a failed upgrade).
    try:
        res = runner.run(["history", release, "-o", "json"], namespace=namespace)
        hist = _parse_history(res.stdout) if res.success else None
        out["recent_revisions"] = hist[-5:] if hist else None
        if not res.success:
            out["errors"]["history"] = (res.stderr or "history failed").strip()
    except HelmError as exc:
        out["recent_revisions"] = None
        out["errors"]["history"] = str(exc)

    # 3. Resources from the rendered manifest.
    resources: List[dict] = []
    try:
        res = runner.run(["get", "manifest", release], namespace=namespace)
        if res.success:
            redacted = redaction.redact_manifest(res.stdout, _HELM_MANIFEST_CAP)
            resources = _extract_manifest_resources(redacted)
        else:
            out["errors"]["manifest"] = (res.stderr or "manifest failed").strip()
    except HelmError as exc:
        out["errors"]["manifest"] = str(exc)
    out["resource_source"] = "manifest"
    out["resource_count"] = len(resources)
    workloads = [r for r in resources if r["kind"] in _WORKLOAD_KINDS]
    out["workloads"] = workloads
    workload_names = {r["name"] for r in workloads}
    resource_keys = {(r["kind"], r["name"]) for r in resources}

    # 4. Live correlation (kubectl, same target), scoped to this release's
    #    resources where possible so a shared namespace's unrelated noise does
    #    not make this release look unhealthy.
    out["pod_health"] = _release_pod_health(namespace, workload_names)
    out["recent_warnings"] = _release_warnings(namespace, resource_keys, workload_names)

    # 5. Health assessment.
    #    - Only the CURRENT release status blocks health; older failed revisions
    #      are historical context, surfaced separately, not health-blocking.
    #    - Unhealthy pods block health only when we could scope them to the release.
    status_obj = out.get("status") or {}
    status_str = status_obj.get("status", "")
    current_rev = status_obj.get("revision")
    prior_failed = [
        r for r in (out.get("recent_revisions") or [])
        if str(r.get("revision")) != str(current_rev)
        and str(r.get("status", "")).lower() in _FAILED_REVISION_STATUSES
    ]
    out["prior_failed_revisions"] = prior_failed or None

    ph = out.get("pod_health") or {}
    pods_block_health = bool(ph.get("scoped")) and (ph.get("unhealthy_count", 0) or 0) > 0
    out["release_healthy"] = (status_str == "deployed") and not pods_block_health
    out["found"] = True
    if not out["errors"]:
        out.pop("errors")
    return out


def _parse_metadata(stdout: str) -> Optional[dict]:
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "name": data.get("name", ""),
        "chart": data.get("chart", ""),
        "version": data.get("version", ""),
        "app_version": data.get("appVersion", ""),
        "namespace": data.get("namespace", ""),
        "revision": data.get("revision", ""),
    }


def _parse_status(stdout: str) -> Optional[dict]:
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return None
    info = data.get("info", {}) if isinstance(data, dict) else {}
    chart = (data.get("chart") or {}).get("metadata", {}) if isinstance(data, dict) else {}
    return {
        "name": data.get("name", ""),
        "namespace": data.get("namespace", ""),
        "revision": data.get("version", ""),
        "status": info.get("status", ""),
        "last_deployed": info.get("last_deployed", ""),
        "description": info.get("description", ""),
        "chart": chart.get("name", ""),
        "chart_version": chart.get("version", ""),
        "app_version": chart.get("appVersion", ""),
    }


def _parse_history(stdout: str) -> Optional[list]:
    try:
        data = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    return [
        {
            "revision": r.get("revision", ""),
            "updated": r.get("updated", ""),
            "status": r.get("status", ""),
            "chart": r.get("chart", ""),
            "app_version": r.get("app_version", ""),
            "description": r.get("description", ""),
        }
        for r in data if isinstance(r, dict)
    ]
