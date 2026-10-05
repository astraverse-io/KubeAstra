"""Deterministic GitOps reconciliation diagnosis — pure and keyless.

    diagnose(obj) -> Diagnosis

Given an Argo CD ``Application`` or a Flux ``Kustomization``/``HelmRelease``
object (the parsed CR, as ``kubectl get ... -o json`` returns it), walk its
status to the first failing resource, classify the root cause from the structured
conditions/messages, and propose a fix route. No LLM, no cluster — the optional
natural-language narration is a separate, caller-side concern.

The classification is rule-based on the controllers' own status fields, which is
more reliable (and testable) than asking a model to guess.
"""
from __future__ import annotations

from typing import Any

from .types import Diagnosis, Evidence, FailingResource, Fix

# Keyword signatures for the common failure modes, matched against the combined
# condition / operationState / resource messages (lower-cased).
_IMAGE = ("imagepullbackoff", "errimagepull", "image can't be pulled", "pull access denied", "manifest unknown")
_RBAC = ("forbidden", "cannot create", "cannot get", "cannot patch", "is not allowed", "rbac")
_CRD = ("no matches for kind", "could not find the requested resource", "ensure crd",
        "unable to recognize", "unknown field", "failed to get api group resources")
_HOOK = ("hook", "presync", "postsync", "syncfail")


def _classify_text(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in _IMAGE):
        return "image_pull"
    if any(k in t for k in _CRD):
        return "crd_schema_mismatch"
    if any(k in t for k in _RBAC):
        return "rbac"
    if any(k in t for k in _HOOK):
        return "failed_hook"
    return ""


def _fix_for(root_cause: str) -> Fix:
    return {
        "out_of_sync_drift": Fix(
            "gitops_pr",
            "Live state drifted from Git. Reconcile through Git — open a PR to update the "
            "manifest (or sync the app once the diff is reviewed), rather than editing the cluster.",
        ),
        "image_pull": Fix(
            "advisory",
            "Fix the image reference or add/repair the imagePullSecret; this is a workload "
            "problem, not a Git drift.",
        ),
        "rbac": Fix(
            "advisory",
            "The controller's ServiceAccount lacks RBAC for a resource it must manage; grant "
            "the missing permission.",
        ),
        "failed_hook": Fix(
            "advisory",
            "A sync hook failed; inspect the hook Job's logs, resolve it, then re-sync.",
        ),
        "crd_schema_mismatch": Fix(
            "advisory",
            "A required CRD is missing or the manifest uses an unknown field; install/upgrade "
            "the CRD (operator) before syncing. (Pairs with the Upgrade Pilot.)",
        ),
        "progressing": Fix("none", "The app is still progressing; wait for it to settle before acting."),
        "healthy": Fix("none", "App is healthy and synced; nothing to do."),
    }.get(root_cause, Fix("advisory", "Inspect the failing resource's events and logs for the root cause."))


def _summary(name: str, root_cause: str, first: FailingResource | None) -> str:
    where = ""
    if first and (first.kind or first.name):
        where = f" at {first.kind}/{first.name}".rstrip("/")
    human = root_cause.replace("_", " ")
    return f"{name}: {human}{where}."


def diagnose(obj: dict[str, Any]) -> Diagnosis:
    """Diagnose a GitOps app object (Argo Application or Flux Kustomization/HelmRelease)."""
    kind = (obj or {}).get("kind", "") or "Application"
    name = ((obj or {}).get("metadata") or {}).get("name", "")
    status = (obj or {}).get("status") or {}
    if kind == "Application":
        return _diagnose_argo(name, status)
    return _diagnose_flux(name, kind, status)


def _diagnose_argo(name: str, status: dict[str, Any]) -> Diagnosis:
    health = (status.get("health") or {}).get("status", "")
    sync = (status.get("sync") or {}).get("status", "")
    evidence: list[Evidence] = []

    for c in status.get("conditions") or []:
        if c.get("message"):
            evidence.append(Evidence("condition", f"{c.get('type', '')}: {c['message']}"))
    op = status.get("operationState") or {}
    if op.get("message"):
        evidence.append(Evidence("operationState", f"{op.get('phase', '')}: {op['message']}"))

    first: FailingResource | None = None
    for r in status.get("resources") or []:
        rh = (r.get("health") or {}).get("status", "")
        rs = r.get("status", "")
        if rh in ("Degraded", "Missing") or rs == "OutOfSync":
            msg = (r.get("health") or {}).get("message", "")
            candidate = FailingResource(
                group=r.get("group", ""), kind=r.get("kind", ""), name=r.get("name", ""),
                namespace=r.get("namespace", ""), health=rh, sync=rs, message=msg,
            )
            if msg:
                evidence.append(Evidence("resource", f"{r.get('kind', '')}/{r.get('name', '')}: {msg}"))
            # Prefer a genuinely broken (Degraded/Missing) resource over merely OutOfSync.
            if first is None or rh in ("Degraded", "Missing"):
                first = candidate
            if rh in ("Degraded", "Missing"):
                break

    root_cause = _classify_text(" ".join(e.detail for e in evidence))
    if not root_cause:
        if health == "Healthy" and sync == "Synced":
            root_cause = "healthy"
        elif sync == "OutOfSync":
            root_cause = "out_of_sync_drift"
        elif health == "Progressing":
            root_cause = "progressing"
        else:
            root_cause = "unknown"

    return Diagnosis(
        app=name, app_kind="Application", health=health, sync=sync, root_cause=root_cause,
        summary=_summary(name, root_cause, first), first_failing=first, evidence=evidence,
        fix=_fix_for(root_cause),
    )


def _diagnose_flux(name: str, kind: str, status: dict[str, Any]) -> Diagnosis:
    ready = None
    evidence: list[Evidence] = []
    for c in status.get("conditions") or []:
        if c.get("type") == "Ready":
            ready = c
        if c.get("message"):
            evidence.append(Evidence("condition", f"{c.get('type', '')}({c.get('reason', '')}): {c['message']}"))

    is_ready = (ready or {}).get("status") == "True"
    health = "Healthy" if is_ready else "Degraded"
    sync = "Synced" if is_ready else "OutOfSync"

    text = " ".join(e.detail for e in evidence) + " " + (ready or {}).get("reason", "")
    root_cause = _classify_text(text)
    if not root_cause:
        if is_ready:
            root_cause = "healthy"
        elif "drift" in text.lower() or "dependency" in text.lower():
            root_cause = "out_of_sync_drift"
        else:
            root_cause = "unknown"

    first = None
    if not is_ready and ready is not None:
        first = FailingResource(kind=kind, name=name, health=health, message=ready.get("message", ""))

    return Diagnosis(
        app=name, app_kind=kind, health=health, sync=sync, root_cause=root_cause,
        summary=_summary(name, root_cause, first), first_failing=first, evidence=evidence,
        fix=_fix_for(root_cause),
    )
