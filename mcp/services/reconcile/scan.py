"""Fetch GitOps app objects for the Reconciliation Pilot.

Pure of kubectl itself: the caller injects a ``run_json(args) -> dict`` runner
(the server passes ``k8s.kubectl_runner.get_runner().run_json``; tests pass a
fake), so fetching is testable without a cluster. Diagnosis then runs on the
returned object via :func:`reconcile.diagnose`.
"""
from __future__ import annotations

from typing import Any

_FLUX_PLURAL = {
    "Kustomization": "kustomizations.kustomize.toolkit.fluxcd.io",
    "HelmRelease": "helmreleases.helm.toolkit.fluxcd.io",
}


def _get(run_json, args: list[str]) -> dict[str, Any]:
    try:
        return run_json(args) or {}
    except Exception:
        return {}


def fetch_argocd_app(run_json, name: str, namespace: str = "argocd") -> dict[str, Any]:
    return _get(run_json, ["get", "applications.argoproj.io", name, "-n", namespace, "-o", "json"])


def fetch_flux(run_json, kind: str, name: str, namespace: str = "flux-system") -> dict[str, Any]:
    plural = _FLUX_PLURAL.get(kind)
    if not plural:
        return {}
    return _get(run_json, ["get", plural, name, "-n", namespace, "-o", "json"])


def fetch_gitops_object(
    run_json, *, kind: str = "Application", name: str, namespace: str = ""
) -> dict[str, Any]:
    """Fetch the CR for an Argo CD Application or a Flux Kustomization/HelmRelease."""
    if kind == "Application":
        return fetch_argocd_app(run_json, name, namespace or "argocd")
    return fetch_flux(run_json, kind, name, namespace or "flux-system")
