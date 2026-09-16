/**
 * Bridge client: the webview's stand-in for `ui/frontend/lib/api.ts`. Instead of
 * fetching the backend directly (blocked by desktop-mode Origin checks and awkward
 * with server-mode HttpOnly cookies), every call is posted to the extension host,
 * which owns the credential and makes the real request. This mirrors the small
 * slice of the api surface the slim chat needs.
 */
import type { HostToWebview } from "./types";

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

/** Streaming request; `onChunk` receives raw SSE text as it arrives. */
export function apiStream(
  path: string,
  body: unknown,
  onChunk: (data: string) => void,
): { done: Promise<number>; abort: () => void } {
  const id = nextId();
  const done = new Promise<number>((resolve, reject) => {
    const off = onHostMessage((msg) => {
      if (msg.type === "api-chunk" && msg.id === id) onChunk(msg.data);
      else if (msg.type === "api-done" && msg.id === id) {
        off();
        resolve(msg.status);
      } else if (msg.type === "api-error" && msg.id === id) {
        off();
        reject(new Error(msg.message));
      }
    });
    vscode.postMessage({ type: "api", id, path, method: "POST", body, stream: true });
  });
  return {
    done,
    abort: () => vscode.postMessage({ type: "api-abort", id }),
  };
}
