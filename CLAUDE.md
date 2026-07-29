# KubeAstra — repository guide

AI-powered Kubernetes investigation assistant. An MCP tool server (51 tools)
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
ui/backend/         FastAPI. main.py adds mcp/ to sys.path — tools run
                    in-process, there is no second daemon.
  routers/          one APIRouter per file, registered in main.py
  desktop_main.py   desktop entry point (main.py has no __main__ block)
  desktop_security.py  localhost/token/origin boundary for desktop mode
ui/frontend/        Next.js App Router. See its own CLAUDE.md — the Next
                    version has breaking changes from training data.
cli/                `kubeastra` CLI (PyPI). `kubeastra open` runs the desktop
                    app from a source checkout.
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

`feat/desktop` is the current example — the desktop app is phases 1–3 and
cannot be installed until Phase 2 produces a signed installer, so it stays off
`main` until then.

Single, self-contained fixes still go straight to `main` via their own PR.

Pushes to `astraverse-io` need the right GitHub account — the active one
drifts back to a work account, and pushes then fail with 403 (or, on the
private planning repo, a misleading `Repository not found`):

```bash
gh auth switch --user pruthviraja
```

Related repos: `astraverse-io/homebrew-tap` (Homebrew cask),
`astraverse-io/kubeastra-internal` (private planning docs).
