# KubeAstra — repository guide

AI-powered Kubernetes investigation assistant. An MCP tool server (56 tools)
plus a FastAPI + Next.js UI. Runs two ways from one codebase, switched by
`KUBEASTRA_MODE`:

- `server` (default) — deployed to a cluster via Helm/docker-compose.
  Multi-user, cookie auth, Qdrant server for RAG memory.
- `desktop` — single-user app on a laptop, using the local kubeconfig.
  No auth accounts; `desktop_security.py` is the boundary instead.

## Planning docs live in a separate PRIVATE repo

Design docs, decision records and implementation specs are **not** in this
repo. They are in `astraverse-io/kubeastra-internal` (private).

If `internal_docs/` is missing or empty in your checkout, set it up:

```bash
git clone https://github.com/astraverse-io/kubeastra-internal.git ~/source/repos/kubeastra-internal
ln -s ~/source/repos/kubeastra-internal internal_docs
```

`internal_docs` is gitignored here, so the symlink is invisible to git.

**Read `internal_docs/START_HERE.md` before starting substantial work.** It
carries project state, every locked decision and its reasoning, and what to do
next. Then:

- `internal_docs/features/DESKTOP_APP_PLAN.md` — desktop architecture, the
  security threat model, and the locked decisions (the *why*).
- `internal_docs/features/DESKTOP_PHASE1_2_SPEC.md` — file-by-file
  instructions for the desktop work in flight.
- `internal_docs/features/FEATURE_ROADMAP.md` — the four non-desktop features.

If you cannot access that repo, say so rather than guessing at the plans.

## Layout

```
mcp/                MCP server; tools, RAG, LLM providers, playbooks
  k8s/              kubectl via subprocess (NOT the Python k8s client)
  services/         llm/, rag/, embeddings, vector_db, plans
    upgrade/        Upgrade Pilot pure core — keyless, deterministic (no ui/DB/LLM)
    reconcile/      GitOps Reconciliation Pilot pure core — keyless Argo/Flux diagnosis
  evals/            offline quality gates (manifest_fix, pilots) — see evals.yml
ui/backend/         FastAPI. main.py adds mcp/ to sys.path — tools run
                    in-process, there is no second daemon.
  routers/          one APIRouter per file, registered in main.py
  pilots/           Pilot registry (tool_scope + flag) + resumable apply runner
  harness/          Agent Harness v2 — native tool-calling loop (AGENT_HARNESS_V2)
  desktop_main.py   desktop entry point (main.py has no __main__ block)
  desktop_security.py  localhost/token/origin boundary for desktop mode
ui/frontend/        Next.js App Router. See its own CLAUDE.md — the Next
                    version has breaking changes from training data.
cli/                `kubeastra` CLI (PyPI). `kubeastra open` runs the desktop
                    app from a source checkout; `kubeastra upgrade` runs the
                    keyless Upgrade Pilot (also shipped as a GitHub Action in action/).
helm/kubeastra/     server-mode deployment
```

## Conventions

- **Backend**: routers in `ui/backend/routers/`, `app.include_router(x.router,
  prefix="/api")`. SQLite through `db._conn()`, no ORM; schema is additive in
  `init_db()`. Auth helpers in `auth.py`.
- **Settings**: pydantic-settings in `mcp/config/settings.py`; plain annotated
  fields, env vars are the upper-snake of the field name.
- **Frontend**: all API calls go through `lib/api.ts`; relative `/api/*` URLs.
- **Tests**: pytest + `TestClient` (`ui/backend/tests/`, `cli/tests/`),
  vitest + testing-library (`ui/frontend`). Keep the baseline green:
  472 backend / 70 frontend / 26 CLI.
  `tests/test_cost_tracking.py` has a pre-existing unrelated collection error.
- **`mcp/services/embeddings.py` exports a module-level `embeddings`
  singleton** imported by 8 call sites. Swap backends behind it; do not
  replace it with a factory.

## Pilots, Agent Harness v2, and Evals

Three subsystems landed recently. All are **additive and off by default**
(opt-in flags in `mcp/config/settings.py`); the existing text-ReAct path and
tool set are unchanged when the flags are off.

- **Pilots** (`ui/backend/pilots/`, cores in `mcp/services/upgrade/` +
  `mcp/services/reconcile/`): scoped, deterministic workflows on the existing
  remediation spine. v1 = **Upgrade Pilot** (version-upgrade readiness + Option-B
  plan-approval + resumable safe apply) and **GitOps Reconciliation** (Argo/Flux
  root-cause). A Pilot is a `tool_scope` + flag + pure core — see
  `ui/backend/pilots/README.md` ("how to add a Pilot"). Flags: `pilots_enabled`,
  `upgrade_pilot_enabled`, `gitops_reconcile_enabled`. Writes reuse the audited
  propose→approve→execute spine; cores import no ui/DB/LLM/web (a test enforces
  this). The Upgrade Pilot also ships as a CLI (`kubeastra upgrade`) and a
  composite GitHub Action (`action/`).
- **Agent Harness v2** (`ui/backend/harness/`): native tool-calling that replaces
  the text-ReAct parse/salvage stack with structured `{name, arguments}` calls.
  Flag `AGENT_HARNESS_V2` (default off); `react_loop` routes to it only when the
  flag is on *and* the provider's `supports_native_tools()` is True (Claude /
  OpenAI / Gemini — Ollama falls back to text-ReAct). Approval-resume always uses
  text-ReAct.
- **Evals** (`mcp/evals/`): offline, deterministic quality gates wired into
  `.github/workflows/evals.yml`. `manifest_fix` (fix-a-manifest golden set) and
  `pilots` (blocking recall/precision, routing, plan ordering, reconcile hit
  rate). Run locally from `mcp/`: `python -m evals.<name>.run`.

## Git

**`main` is protected and enforced — direct pushes are rejected** (`GH013`).
The `main-protection` ruleset requires pull requests, requires linear history,
and blocks force-pushes and deletion. The repo-admin bypass was removed on
2026-07-29 after several commits landed directly while it only printed
`Bypassed rule violations`.

Work on a branch and open a PR. Zero approvals are required, so a solo
maintainer can merge immediately:

```bash
git checkout -b fix/thing && git push -u origin fix/thing
gh pr create --fill && gh pr merge --squash --delete-branch
```

### Multi-phase work stays off `main`

**`main` must stay releasable.** This repo is public; `main` is what people
read and clone. A feature that spans phases does NOT go to `main` one phase at
a time, even when every phase is tested and green — tested is not the same as
shippable, and a half-built feature on a public trunk advertises something
nobody can use.

Instead, keep a long-lived integration branch and merge to `main` only when
the whole thing ships:

```
feat/desktop            long-lived; phases land here via PR
  └─ feat/desktop-p2    short-lived work branch -> PR into feat/desktop
```

`feat/desktop` was the worked example: the desktop app spanned phases 1–3 and
could not be installed until Phase 2 produced a signed installer, so it stayed
off `main` for that whole time. It **merged on 2026-08-11** (#70), once
`desktop-v0.2.0` had produced a DMG that Gatekeeper accepts as
`Notarized Developer ID`. The rule held: `main` never advertised an app nobody
could install.

`feat/desktop` has since been deleted (its work shipped; the stale branch was
pruned). The pattern was reused for the next multi-phase features, each kept on a
long-lived integration branch and merged only when the whole thing shipped:
the **Desktop Agent** (read + validated-write, then native-harness + evals —
#89, #97) and the **Pilots** program (Upgrade + GitOps Reconciliation, Phases
0–6 — #98). Apply the same pattern to the next one.

Single, self-contained fixes still go straight to `main` via their own PR.

Pushes to `astraverse-io` need the right GitHub account — the active one
drifts back to a work account, and pushes then fail with 403 (or, on the
private planning repo, a misleading `Repository not found`):

```bash
gh auth switch --user pruthviraja
```

Related repos: `astraverse-io/homebrew-tap` (Homebrew cask),
`astraverse-io/kubeastra-internal` (private planning docs).
