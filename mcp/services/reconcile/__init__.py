"""KubeAstra GitOps Reconciliation Pilot — pure diagnosis core.

Dependency-free by design (no ``ui/``, no LLM, no DB, no web): given an Argo CD
``Application`` or a Flux ``Kustomization``/``HelmRelease`` object, it classifies
why the app is ``OutOfSync``/``Degraded`` and proposes a fix route — a reviewed
GitOps PR for drift (pairing with #81), or an advisory for operational causes.
See ``internal_docs/features/PILOTS_PLAN.md`` §Phase 4.
"""
from .types import Diagnosis, Evidence, FailingResource, Fix
from .analyze import diagnose
from .scan import fetch_argocd_app, fetch_flux, fetch_gitops_object

__all__ = [
    "diagnose",
    "fetch_argocd_app",
    "fetch_flux",
    "fetch_gitops_object",
    "Diagnosis",
    "Evidence",
    "FailingResource",
    "Fix",
]
