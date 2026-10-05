import { describe, it, expect } from "vitest";
import { ChatSseParser } from "../sse";
import type { ChatStreamEvent } from "@lib/api";

/** Encode events as an SSE wire string, then hand `feed` arbitrary slices of it. */
function frames(...events: object[]): string {
  return events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("");
}

function collect(wire: string, chunkSize: number) {
  const seen: ChatStreamEvent[] = [];
  const p = new ChatSseParser((e) => seen.push(e));
  for (let i = 0; i < wire.length; i += chunkSize) p.feed(wire.slice(i, i + chunkSize));
  return { seen, p };
}

describe("ChatSseParser", () => {
  const wire = frames(
    { type: "start" },
    { type: "iteration_planned", iteration: 1, action: "get_pods" },
    { type: "token", text: "Hello " },
    { type: "token", text: "world" },
    { type: "done", result: { reply: "Hello world", tool_used: "get_pods", result: null } },
  );

  it("parses a full stream fed in one chunk", () => {
    const { seen, p } = collect(wire, wire.length);
    expect(seen.map((e) => e.type)).toEqual([
      "start",
      "iteration_planned",
      "token",
      "token",
      "done",
    ]);
    expect(p.finalResult?.reply).toBe("Hello world");
    expect(p.errorMessage).toBeNull();
  });

  it("reassembles frames split across arbitrary chunk boundaries", () => {
    for (const size of [1, 3, 7, 13]) {
      const { seen, p } = collect(wire, size);
      const tokens = seen.filter((e) => e.type === "token").map((e) => e.text);
      expect(tokens.join("")).toBe("Hello world");
      expect(p.finalResult?.reply).toBe("Hello world");
    }
  });

  it("captures an error event", () => {
    const { p } = collect(frames({ type: "error", message: "boom" }), 4);
    expect(p.errorMessage).toBe("boom");
    expect(p.finalResult).toBeNull();
  });

  it("ignores a malformed frame but keeps parsing later ones", () => {
    const bad = "data: {not json}\n\n" + frames({ type: "done", result: { reply: "ok", tool_used: "", result: null } });
    const { seen, p } = collect(bad, 5);
    expect(seen.map((e) => e.type)).toEqual(["done"]);
    expect(p.finalResult?.reply).toBe("ok");
  });

  it("does not emit a frame until its terminating blank line arrives", () => {
    const seen: ChatStreamEvent[] = [];
    const p = new ChatSseParser((e) => seen.push(e));
    p.feed('data: {"type":"token","text":"partial"}'); // no blank line yet
    expect(seen).toHaveLength(0);
    p.feed("\n\n");
    expect(seen).toHaveLength(1);
    expect(seen[0].text).toBe("partial");
  });
});
