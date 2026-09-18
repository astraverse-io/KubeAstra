import * as vscode from "vscode";
import { AuthManager } from "./auth";
import { authedFetch } from "./http";
import type { ClusterInfo } from "./protocol";

const POLL_MS = 30_000; // matches the web app's cluster poll cadence

/**
 * Polls the backend's cluster reachability via `/api/cluster/autodetect` and
 * publishes it. The host is the single poller; the status bar and the webview
 * header both consume `onChange` rather than each fetching on their own.
 *
 * autodetect is session-less and reports whether the backend can see a cluster
 * (in-cluster ServiceAccount, or a kubeconfig with a current context) — an
 * honest "is a cluster attached" signal without needing a chat session.
 */
export class ClusterMonitor {
  private timer: ReturnType<typeof setInterval> | undefined;
  private state: ClusterInfo = { connected: false };
  private readonly emitter = new vscode.EventEmitter<ClusterInfo>();
  readonly onChange = this.emitter.event;

  constructor(private readonly auth: AuthManager) {}

  get current(): ClusterInfo {
    return this.state;
  }

  start(): void {
    void this.refresh();
    this.timer = setInterval(() => void this.refresh(), POLL_MS);
  }

  dispose(): void {
    if (this.timer) clearInterval(this.timer);
    this.emitter.dispose();
  }

  async refresh(): Promise<void> {
    const base = this.auth.backendUrl();
    if (!base) {
      this.set({ connected: false });
      return;
    }
    try {
      const res = await authedFetch(base, "/api/cluster/autodetect", await this.auth.getCookie());
      if (!res.ok) {
        this.set({ connected: false });
        return;
      }
      this.set(mapAutodetect((await res.json()) as AutodetectResponse));
    } catch {
      this.set({ connected: false });
    }
  }

  private set(next: ClusterInfo): void {
    if (
      next.connected === this.state.connected &&
      next.name === this.state.name &&
      next.context === this.state.context
    ) {
      return; // no change; don't churn subscribers
    }
    this.state = next;
    this.emitter.fire(next);
  }
}

interface AutodetectResponse {
  in_cluster?: boolean;
  current_context?: string;
  contexts?: unknown[];
}

/** Map the autodetect payload to our minimal cluster status. */
export function mapAutodetect(d: AutodetectResponse): ClusterInfo {
  if (d.in_cluster) return { connected: true, name: "in-cluster" };
  const context = d.current_context;
  const hasContexts = Array.isArray(d.contexts) && d.contexts.length > 0;
  if (context || hasContexts) {
    return { connected: true, name: context, context };
  }
  return { connected: false };
}
