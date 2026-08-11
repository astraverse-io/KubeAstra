/**
 * The pending-shared-session key must be consumed on every auth path.
 *
 * The chat page stashes a session id in sessionStorage, then two effects race
 * on it:
 *
 *   - one consumes it and sets historyLoaded
 *   - the other bails out early *while the key is present*, so it never
 *     loads history itself
 *
 * The consumer only ran when `auth_enabled && user`. Desktop mode has auth
 * disabled, so returning from Alerts left the key set forever: the second
 * effect kept bailing, historyLoaded never flipped, and the app sat on
 * "Loading history…" with the chat unreachable. Reported against 0.2.2.
 *
 * This is a source-level check. Rendering the chat page needs the whole API
 * surface mocked, and the failure is an effect-ordering deadlock rather than
 * anything a shallow render would surface — the page renders fine, it just
 * never leaves the loading state.
 */

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const SOURCE = readFileSync(
  join(__dirname, "..", "..", "app", "chat", "page.tsx"),
  "utf8",
);

describe("pending shared session", () => {
  it("is consumed when auth is disabled, not only when signed in", () => {
    // The branch that runs in desktop mode and local dev.
    const branch = SOURCE.split("!status.auth_enabled && typeof window")[1];

    expect(branch, "the auth-disabled branch is gone; re-check this test").toBeTruthy();

    const upToNextBranch = branch.split("} else {")[0];
    expect(
      upToNextBranch,
      "loadPendingSharedSession is not called when auth is disabled — the " +
        "key stays set, the history effect bails on its presence, and the " +
        'app hangs on "Loading history…"',
    ).toContain("loadPendingSharedSession");
  });

  it("the early-return that depends on the key still exists", () => {
    // If this guard is ever removed the deadlock cannot happen, and the test
    // above is protecting nothing — better to know than to keep asserting.
    expect(SOURCE).toContain(
      "sessionStorage.getItem(PENDING_SHARED_SESSION_KEY)) return",
    );
  });

  it("every exit from the consumer clears the key", () => {
    // Both its success and failure paths must remove the key and set
    // historyLoaded, or calling it simply moves the hang rather than fixing it.
    const fn = SOURCE.split("const loadPendingSharedSession")[1].split(
      "}, []);",
    )[0];
    const clears = fn.split("sessionStorage.removeItem(PENDING_SHARED_SESSION_KEY)")
      .length - 1;
    const loaded = fn.split("setHistoryLoaded(true)").length - 1;

    expect(clears, "success and failure paths must both clear the key").toBeGreaterThanOrEqual(2);
    expect(loaded, "success and failure paths must both end the loading state").toBeGreaterThanOrEqual(2);
  });
});
