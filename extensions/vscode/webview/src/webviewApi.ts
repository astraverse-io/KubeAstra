/**
 * Bridge client: the webview's stand-in for `ui/frontend/lib/api.ts`. Instead of
 * fetching the backend directly (blocked by desktop-mode Origin checks and awkward
 * with server-mode HttpOnly cookies), every call is posted to the extension host,
 * which owns the credential and makes the real request. This mirrors the small
 * slice of the api surface the slim chat needs.
 */
import type { HostToWebview } from "./types";
import type { ChatStreamEvent, ChatResponse, ChatMessage, ExecuteResponse } from "@lib/api";
import { ChatSseParser } from "./sse";

interface VsCodeApi {
  postMessage(msg: unknown): void;
}
declare function acquireVsCodeApi(): VsCodeApi;

const vscode = acquireVsCodeApi();

let seq = 0;
const nextId = () => `${Date.now()}-${seq++}`;

type Listener = (msg: HostToWebview) => void;
const listeners = new Set<Listener>();

window.addEventListener("message", (e: MessageEvent<HostToWebview>) => {
  for (const l of listeners) l(e.data);
});

/** Subscribe to raw host→webview messages (auth-state, prompt, …). Returns an unsubscribe. */
export function onHostMessage(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** Tell the host the webview has mounted and wants initial state. */
export function ready(): void {
  vscode.postMessage({ type: "ready" });
}

/** One-shot JSON request, resolved by the host's api-response. */
export function apiRequest<T = unknown>(
  path: string,
  opts: { method?: string; body?: unknown } = {},
): Promise<{ status: number; body: T }> {
  const id = nextId();
  return new Promise((resolve, reject) => {
    const off = onHostMessage((msg) => {
      if (msg.type === "api-response" && msg.id === id) {
        off();
        resolve({ status: msg.status, body: msg.body as T });
      } else if (msg.type === "api-error" && msg.id === id) {
        off();
        reject(new Error(msg.message));
      }
    });
    vscode.postMessage({ type: "api", id, path, method: opts.method, body: opts.body });
  });
}

/** An Error marking a caller-initiated abort, so callers can ignore it quietly. */
export function isAbortError(err: unknown): boolean {
  return err instanceof Error && err.name === "AbortError";
}

/**
 * Streaming request; `onChunk` receives raw SSE text as it arrives.
 *
 * The host does not echo a terminal message when it honours an abort (its pump
 * simply stops), so `abort()` must clean up on the webview side: unsubscribe the
 * listener and settle `done` itself. Otherwise every aborted stream would leak a
 * listener and leave `done` pending forever.
 */
export function apiStream(
  path: string,
  body: unknown,
  onChunk: (data: string) => void,
): { done: Promise<number>; abort: () => void } {
  const id = nextId();
  let settled = false;
  let resolveDone!: (status: number) => void;
  let rejectDone!: (err: Error) => void;
  const done = new Promise<number>((resolve, reject) => {
    resolveDone = resolve;
    rejectDone = reject;
  });

  const off = onHostMessage((msg) => {
    if (msg.type === "api-chunk" && msg.id === id) onChunk(msg.data);
    else if (msg.type === "api-done" && msg.id === id) finish(() => resolveDone(msg.status));
    else if (msg.type === "api-error" && msg.id === id) finish(() => rejectDone(new Error(msg.message)));
  });

  function finish(settle: () => void): void {
    if (settled) return;
    settled = true;
    off();
    settle();
  }

  vscode.postMessage({ type: "api", id, path, method: "POST", body, stream: true });

  return {
    done,
    abort: () => {
      if (settled) return;
      vscode.postMessage({ type: "api-abort", id });
      const err = new Error("aborted");
      err.name = "AbortError";
      finish(() => rejectDone(err));
    },
  };
}

/**
 * Stream a chat turn. Mirrors `ui/frontend/lib/api.ts` `sendChatStream`: posts to
 * `/api/chat/stream` through the host bridge and parses the raw SSE chunks the
 * host forwards (frames split on a blank line, one or more `data:` lines each)
 * into typed `ChatStreamEvent`s. Resolves with the final `ChatResponse` from the
 * `done` event, or rejects on an `error` event / transport failure.
 */
export function sendChatStream(
  message: string,
  history: ChatMessage[],
  onEvent: (event: ChatStreamEvent) => void,
): { result: Promise<ChatResponse>; abort: () => void } {
  const parser = new ChatSseParser(onEvent);
  const { done, abort } = apiStream("/api/chat/stream", { message, history }, (c) => parser.feed(c));
  const result = done.then(() => {
    if (parser.errorMessage) throw new Error(parser.errorMessage);
    if (!parser.finalResult) throw new Error("stream ended without a 'done' event");
    return parser.finalResult;
  });
  return { result, abort };
}

/** Execute a command (the approval-overlay confirm path). `confirm` gates writes. */
export async function executeCommand(command: string, confirm = false): Promise<ExecuteResponse> {
  const { body } = await apiRequest<ExecuteResponse>("/api/execute", {
    method: "POST",
    body: { command, confirm },
  });
  return body;
}
