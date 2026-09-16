import * as vscode from "vscode";
import { AuthManager } from "./auth";

/**
 * A status-bar item reflecting KubeAstra connection state. Clicking it signs in
 * (or focuses the chat when already connected). Refreshes on auth changes.
 */
export function registerStatusBar(ctx: vscode.ExtensionContext, auth: AuthManager): void {
  const item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  ctx.subscriptions.push(item);

  const refresh = async () => {
    const base = auth.backendUrl();
    if (!base) {
      item.text = "$(cloud) KubeAstra: set backend";
      item.tooltip = "Click to configure and sign in to a KubeAstra backend";
      item.command = "kubeastra.signIn";
    } else if (await auth.isSignedIn()) {
      item.text = "$(check) KubeAstra";
      item.tooltip = `Connected to ${base}`;
      item.command = "kubeastra.ask";
    } else {
      item.text = "$(sign-in) KubeAstra: sign in";
      item.tooltip = `Sign in to ${base}`;
      item.command = "kubeastra.signIn";
    }
    item.show();
  };

  ctx.subscriptions.push(auth.onChange(() => void refresh()));
  void refresh();
}
