/**
 * Session links must use `/chat?session=<id>`, never `/chat/<id>`.
 *
 * `/chat/<id>` is a dynamic route. A dynamic route cannot exist in the desktop
 * static export — session ids are not knowable at build time, so
 * generateStaticParams() has nothing to enumerate — and build-desktop.mjs
 * stashes the back-compat page aside entirely. The server build forwards the
 * old shape, so this is invisible there.
 *
 * The alerts page shipped with the path form in 0.2.1. "Back to chat" landed
 * on "404 This page could not be found" in the installed app, with the chat
 * still running behind it. Every unit test passed, the build was clean, and
 * the export contained exactly what it was asked to contain — the route
 * simply did not exist.
 *
 * A source-level check because the failure only appears in a built export,
 * and nothing renders these navigations in a test.
 */

import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const ROOT = join(__dirname, "..", "..");
const SEARCH_DIRS = ["app", "components", "lib"];
const CODE = /\.(tsx?|jsx?)$/;

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  let entries: string[];
  try {
    entries = readdirSync(dir);
  } catch {
    return out;
  }
  for (const entry of entries) {
    if (entry === "node_modules" || entry === "__tests__" || entry.startsWith(".")) continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...sourceFiles(full));
    else if (CODE.test(entry)) out.push(full);
  }
  return out;
}

// `/chat/` followed by an interpolation or concatenation — i.e. a session id
// being pushed into the path. A literal `/chat/` with a static segment after
// it is a different thing and not what broke.
const PATH_FORM = /["'`]\/chat\/\$\{|["']\/chat\/["']\s*\+|`\/chat\/\$/;

describe("chat session links", () => {
  const files = SEARCH_DIRS.flatMap((d) => sourceFiles(join(ROOT, d)));

  it("finds source files to check", () => {
    // Without this, a broken path walker makes every assertion below vacuous.
    expect(files.length).toBeGreaterThan(20);
  });

  it("never builds a session link as a path segment", () => {
    const offenders = files
      .filter((f) => !f.includes(join("chat", "[sessionId]")))
      .filter((f) => PATH_FORM.test(readFileSync(f, "utf8")))
      .map((f) => f.slice(ROOT.length + 1));

    expect(
      offenders,
      "these build `/chat/<id>`, which 404s in the desktop static export — " +
        "use `/chat?session=<id>` instead",
    ).toEqual([]);
  });

  it("the alerts page returns to chat in the query form", () => {
    const source = readFileSync(join(ROOT, "app", "alerts", "page.tsx"), "utf8");

    expect(source).toContain("/chat?session=");
    expect(source).toContain("encodeURIComponent(returnSession)");
  });
});
