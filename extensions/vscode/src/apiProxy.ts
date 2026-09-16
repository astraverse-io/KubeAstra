import type { AuthManager } from "./auth";
import type { ApiRequest, HostToWebview } from "./protocol";

type Post = (msg: HostToWebview) => void;

/**
 * Proxies backend calls on behalf of the webview. Owns the session cookie and
 * is the only place in the extension that hits the network. Streaming requests
 * (the chat SSE endpoint, `Accept: text/event-stream`, read as a chunked
 * `ReadableStream` in the web app) are forwarded chunk-by-chunk so the webview
 * can render tokens as they arrive.
 */
export class ApiProxy {
  private readonly inflight = new Map<string, AbortController>();

  constructor(private readonly auth: AuthManager) {}

  abort(id: string): void {
    this.inflight.get(id)?.abort();
    this.inflight.delete(id);
  }

  abortAll(): void {
    for (const c of this.inflight.values()) c.abort();
    this.inflight.clear();
  }

  async handle(req: ApiRequest, post: Post): Promise<void> {
    const base = this.auth.backendUrl();
    if (!base) {
      post({ type: "api-error", id: req.id, message: "No backend URL configured" });
      return;
    }

    const controller = new AbortController();
    this.inflight.set(req.id, controller);

    const cookie = await this.auth.getCookie();
    const headers: Record<string, string> = {};
    if (cookie) headers.cookie = cookie;
    if (req.body !== undefined) headers["content-type"] = "application/json";
    if (req.stream) headers.accept = "text/event-stream";

    let res: Response;
    try {
      res = await fetch(`${base}${req.path}`, {
        method: req.method ?? (req.body !== undefined ? "POST" : "GET"),
        headers,
        body: req.body !== undefined ? JSON.stringify(req.body) : undefined,
        signal: controller.signal,
      });
    } catch (err) {
      this.inflight.delete(req.id);
      if ((err as Error).name === "AbortError") return;
      post({ type: "api-error", id: req.id, message: `backend unreachable: ${(err as Error).message}` });
      return;
    }

    if (req.stream && res.body) {
      await this.pump(req.id, res, post);
      this.inflight.delete(req.id);
      return;
    }

    const body = await res.json().catch(() => null);
    this.inflight.delete(req.id);
    post({ type: "api-response", id: req.id, status: res.status, body });
  }

  private async pump(id: string, res: Response, post: Post): Promise<void> {
    const reader = res.body!.getReader();
    const decoder = new TextDecoder();
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        post({ type: "api-chunk", id, data: decoder.decode(value, { stream: true }) });
      }
      const tail = decoder.decode();
      if (tail) post({ type: "api-chunk", id, data: tail });
      post({ type: "api-done", id, status: res.status });
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      post({ type: "api-error", id, message: (err as Error).message });
    }
  }
}
