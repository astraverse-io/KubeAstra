// @vitest-environment jsdom
import { describe, it, expect, beforeEach } from "vitest";

// acquireVsCodeApi must exist before the module's top-level `const vscode = …`.
const posted: any[] = [];
(globalThis as any).acquireVsCodeApi = () => ({
  postMessage: (m: unknown) => posted.push(m),
});

const { apiStream, isAbortError } = await import("../webviewApi");

/** Deliver a host→webview message into the module's window listener. */
function fire(data: unknown) {
  window.dispatchEvent(new MessageEvent("message", { data }));
}

describe("apiStream lifecycle", () => {
  beforeEach(() => {
    posted.length = 0;
  });

  it("streams chunks then resolves on api-done and unsubscribes", async () => {
    const chunks: string[] = [];
    const { done } = apiStream("/api/chat/stream", { m: 1 }, (c) => chunks.push(c));
    const id = posted.at(-1).id as string;

    fire({ type: "api-chunk", id, data: "he" });
    fire({ type: "api-chunk", id, data: "llo" });
    fire({ type: "api-done", id, status: 200 });

    await expect(done).resolves.toBe(200);

    // Listener is gone: a late chunk for the same id is ignored.
    fire({ type: "api-chunk", id, data: "late" });
    expect(chunks).toEqual(["he", "llo"]);
  });

  it("abort rejects done as AbortError, posts api-abort, and unsubscribes", async () => {
    const chunks: string[] = [];
    const { done, abort } = apiStream("/api/chat/stream", {}, (c) => chunks.push(c));
    const id = posted.at(-1).id as string;

    abort();
    expect(posted.some((m) => m.type === "api-abort" && m.id === id)).toBe(true);

    let caught: unknown;
    await done.catch((e) => (caught = e));
    expect(isAbortError(caught)).toBe(true);

    // A late api-done after abort must not throw or double-settle, and the
    // listener must already be removed (no chunk delivered).
    fire({ type: "api-chunk", id, data: "late" });
    fire({ type: "api-done", id, status: 200 });
    expect(chunks).toEqual([]);
  });

  it("abort after settle is a no-op (no second api-abort)", async () => {
    const { done, abort } = apiStream("/api/chat/stream", {}, () => {});
    const id = posted.at(-1).id as string;
    fire({ type: "api-done", id, status: 200 });
    await done;

    posted.length = 0;
    abort();
    expect(posted).toEqual([]);
  });
});
