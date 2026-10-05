import type { ChatStreamEvent, ChatResponse } from "@lib/api";

/**
 * Incremental SSE parser for the chat stream, kept pure and transport-free so it
 * can be unit-tested. Mirrors `ui/frontend/lib/api.ts`: frames are separated by a
 * blank line, each frame has one or more `data:` lines whose values concatenate
 * into one JSON `ChatStreamEvent`. Chunk boundaries are arbitrary — a frame may
 * be split across chunks — so bytes are buffered until a blank line is seen.
 */
export class ChatSseParser {
  private buffer = "";
  finalResult: ChatResponse | null = null;
  errorMessage: string | null = null;

  constructor(private readonly onEvent: (event: ChatStreamEvent) => void) {}

  feed(chunk: string): void {
    this.buffer += chunk;
    let sep = this.buffer.indexOf("\n\n");
    while (sep !== -1) {
      const raw = this.buffer.slice(0, sep);
      this.buffer = this.buffer.slice(sep + 2);
      sep = this.buffer.indexOf("\n\n");
      this.handleFrame(raw);
    }
  }

  private handleFrame(raw: string): void {
    const dataLines: string[] = [];
    for (const line of raw.split("\n")) {
      if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
    }
    if (dataLines.length === 0) return;

    let evt: ChatStreamEvent;
    try {
      evt = JSON.parse(dataLines.join("\n")) as ChatStreamEvent;
    } catch {
      return; // ignore malformed frames; keep the stream alive
    }
    try {
      this.onEvent(evt);
    } catch {
      // consumer errors must not break the loop
    }
    if (evt.type === "done" && evt.result) this.finalResult = evt.result;
    else if (evt.type === "error") this.errorMessage = evt.message ?? "stream error";
  }
}
