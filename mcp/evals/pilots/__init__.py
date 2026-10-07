"""Pilots eval suite (PILOTS_PLAN §Phase 6).

Offline, deterministic quality gate for the keyless Pilot cores:

- **Upgrade** — from labeled cluster snapshots: blocking-detection recall/precision,
  per-item routing correctness, and plan ordering (Kendall-τ vs the known-good
  sequence).
- **Reconciliation** — from labeled Argo/Flux status objects: root-cause hit rate.

Pure functions over ``services.upgrade`` / ``services.reconcile`` — no cluster,
no LLM. The cluster-in-the-loop safe-apply eval (on a ``kind`` cluster) is
deferred with Phase 5 part 3. Run from the ``mcp`` dir: ``python -m evals.pilots.run``.
"""
