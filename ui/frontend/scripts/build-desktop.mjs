#!/usr/bin/env node
/**
 * Build the static export used by desktop mode.
 *
 * Two routes cannot exist in an `output: "export"` build and are moved aside
 * for the duration of the build (restored on success, failure, or Ctrl+C):
 *
 * 1. `app/api/[...path]` — a dev/server-only proxy forwarding /api/* to the
 *    FastAPI backend. Declares `force-dynamic`, which export rejects:
 *      Error: export const dynamic = "force-dynamic" on page "/api/[...path]"
 *             cannot be used with "output: export"
 *    Desktop does not need it: FastAPI serves this export from the same
 *    origin, so relative /api/* URLs already reach the backend.
 *
 * 2. `app/chat/[sessionId]` — the shared-session landing page. Export
 *    requires `generateStaticParams()`, and session IDs cannot be enumerated
 *    at build time:
 *      Error: Page "/chat/[sessionId]" is missing "generateStaticParams()"
 *    Shared links are a multi-user/server feature; in desktop mode there is
 *    no shared deployment to open them against. KNOWN PHASE 0 LIMITATION —
 *    Phase 1 should carry the session ID as a query param (/chat?session=…)
 *    so the feature works in both builds from one static page.
 *
 * 3. `app/forgot-password`, `app/reset-password` — password-reset flows for
 *    multi-user server deployments. `/reset-password` awaits `searchParams`,
 *    which export cannot resolve at build time:
 *      Error: Route /reset-password with `dynamic = "error"` couldn't be
 *             rendered statically because it used `await searchParams`
 *    Desktop runs with auth disabled and a single implicit user, so there is
 *    no account to reset a password for — these are dead routes here rather
 *    than casualties of the export.
 *
 * Next.js has no config-level way to drop a single route, hence the stash.
 */

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, renameSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const frontendDir = dirname(dirname(fileURLToPath(import.meta.url)));
const stashRoot = join(frontendDir, ".desktop-build-stash");

// Relative to app/; each is moved to .desktop-build-stash/<flattened name>.
const EXCLUDED_ROUTES = [
  "api",
  join("chat", "[sessionId]"),
  "forgot-password",
  "reset-password",
];

const stashed = [];

function stash() {
  mkdirSync(stashRoot, { recursive: true });
  for (const relative of EXCLUDED_ROUTES) {
    const from = join(frontendDir, "app", relative);
    if (!existsSync(from)) continue;
    const to = join(stashRoot, relative.replace(/[/\\]/g, "__"));
    rmSync(to, { recursive: true, force: true });
    renameSync(from, to);
    stashed.push({ from, to });
  }
}

function restore() {
  while (stashed.length) {
    const { from, to } = stashed.pop();
    rmSync(from, { recursive: true, force: true });
    mkdirSync(dirname(from), { recursive: true });
    renameSync(to, from);
  }
  rmSync(stashRoot, { recursive: true, force: true });
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    restore();
    process.exit(1);
  });
}

try {
  stash();
  execFileSync("npx", ["next", "build"], {
    cwd: frontendDir,
    stdio: "inherit",
    env: { ...process.env, KUBEASTRA_BUILD_TARGET: "desktop" },
  });
} catch (error) {
  restore();
  console.error("\ndesktop build failed:", error.message);
  process.exit(1);
} finally {
  restore();
}

console.log("\nStatic export written to ui/frontend/out — serve it with `kubeastra open`.");
