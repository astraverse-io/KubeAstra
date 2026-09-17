import * as vscode from "vscode";
import { AuthManager } from "./auth";
import { ClusterMonitor } from "./cluster";

/**
 * A status-bar item reflecting KubeAstra connection state, in priority order:
 * no backend → sign-in required → cluster state (connected + context name, or
 * "no cluster"). Clicking signs in when needed, otherwise focuses the chat.
 * Refreshes on auth and cluster changes.
 */
export function registerStatusBar(
  ctx: vscode.ExtensionContext,
  auth: AuthManager,
  cluster: ClusterMonitor,
): void {
  const item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  ctx.subscriptions.push(item);

  const refresh = async () => {
    const base = auth.backendUrl();
    if (!base) {
      item.text = "$(cloud) KubeAstra: set backend";
      item.tooltip = "Click to configure and sign in to a KubeAstra backend";
      item.command = "kubeastra.signIn";
    } else if (!(await auth.isSignedIn())) {
      item.text = "$(sign-in) KubeAstra: sign in";
      item.tooltip = `Sign in to ${base}`;
      item.command = "kubeastra.signIn";
    } else if (cluster.current.connected) {
      const name = cluster.current.name ?? "cluster";
      item.text = `$(vm-active) KubeAstra: ${name}`;
      item.tooltip = `Connected to ${name} via ${base}`;
      item.command = "kubeastra.ask";
    } else {
      item.text = "$(vm-outline) KubeAstra: no cluster";
      item.tooltip = `Signed in to ${base}; no cluster detected`;
      item.command = "kubeastra.ask";
    }
    item.show();
  };

  ctx.subscriptions.push(auth.onChange(() => void refresh()));
  ctx.subscriptions.push(cluster.onChange(() => void refresh()));
  void refresh();
}
