# Pilots — how to add one

A **Pilot** is a scoped, deterministic workflow layered on KubeAstra's existing
remediation spine: a `tool_scope` (which tools the agent may call), a narration
prompt, optional write `actions`, and a feature flag. Adding one is a
`register(Pilot(...))` call plus a pure core — **not** a re-architecture.

Two ship today: **`upgrade`** (Kubernetes version-upgrade readiness + safe apply)
and **`gitops_reconcile`** (Argo/Flux sync-failure root-cause). Full design:
`internal_docs/features/PILOTS_PLAN.md`.

## The anatomy

| Piece | Where | Rule |
|---|---|---|
| **Pure core** | `mcp/services/<pilot>/` | No `ui/`, DB, LLM, or web imports — keyless, deterministic, unit-tested. LLM (if any) is optional prose only. |
| **Registration** | `ui/backend/pilots/__init__.py` | `register(Pilot(name, tool_scope, narration_prompt, actions, flag, description))` |
| **Flag** | `mcp/config/settings.py` | One bool per pilot, **default `False`**; gate the pilot behind it. |
| **Router wiring** | `ui/backend/routers/pilots.py` | `POST /api/v1/pilots/{name}/run` dispatches to the core; validate every k8s identifier. |
| **Writes (optional)** | existing remediation spine | Reuse propose→approve→execute; never add a new write path. |
| **Eval** | `mcp/evals/pilots/cases/` | Add labeled golden cases; the offline eval gates quality (CI). |

## Steps

1. **Build the pure core** under `mcp/services/<pilot>/` — plain functions over
   plain dataclasses, no side effects. This is the testable heart.
2. **Register the pilot** in `ui/backend/pilots/__init__.py`:
   ```python
   register(Pilot(
       name="my_pilot",
       tool_scope=frozenset({"get_pods", "get_events", ...}),
       narration_prompt="…",
       actions=frozenset(),          # write actions, if any (else read-only)
       flag="my_pilot_enabled",
       description="One line for the Pilots list.",
   ))
   ```
   Tool scoping is enforced via `scoped_tool_names(pilot, valid)`, and the native
   harness reads the same set through `build_native_tool_specs(allowed_tools=…)` —
   so a Pilot is also a native sub-agent scope for free.
3. **Add the flag** to `settings.py` (default `False`) and gate the pilot with it
   (`enabled_pilots(settings)` filters on it).
4. **Wire the route** in `routers/pilots.py`; validate app/namespace/kind against
   the `_SAFE_K8S_NAME` pattern before any `kubectl`.
5. **Add golden eval cases** under `mcp/evals/pilots/cases/` and, if the pilot
   needs new metrics, extend `evals/pilots/scoring.py`. The offline eval
   (`python -m evals.pilots.run`) must stay perfect on the golden set — it's a CI
   gate (`.github/workflows/evals.yml`).
6. **Writes** reuse the remediation spine (propose → admin approve → execute via
   `k8s.wrappers`), with Option-B plan-approval for non-high-risk step batches.
   Do not introduce a second write path.

## Invariants

- **Keyless core:** `mcp/services/<pilot>/` imports nothing from `ui/`, the DB,
  an LLM, or the web. A test asserts this boundary.
- **Default off:** every pilot ships behind a `False` flag until its eval is green.
- **No new write path:** writes always go through the audited remediation spine.
- **Deterministic eval:** golden cases must score perfectly; a drop is a regression.
