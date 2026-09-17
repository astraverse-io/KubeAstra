import * as vscode from "vscode";
import { AuthManager } from "./auth";
import { ApiProxy } from "./apiProxy";
import type { HostToWebview, WebviewToHost } from "./protocol";

/**
 * Hosts the Mission Control chat webview. Loads the Vite bundle from
 * `webview/dist`, locks it down with a CSP that only permits our own scripts
 * (nonce) and inline styles (the Mission Control components use inline styles),
 * and routes messages between the webview and the ApiProxy/AuthManager.
 */
export class KubeAstraChatViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "kubeastra.chat";
  private view?: vscode.WebviewView;

  constructor(
    private readonly ctx: vscode.ExtensionContext,
    private readonly auth: AuthManager,
    private readonly proxy: ApiProxy,
  ) {
    // Re-broadcast auth changes so the webview refreshes its state.
    ctx.subscriptions.push(auth.onChange(() => void this.broadcastAuthState()));
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.ctx.extensionUri, "webview", "dist")],
    };
    view.webview.html = this.getHtml(view.webview);

    view.webview.onDidReceiveMessage((msg: WebviewToHost) => this.onMessage(msg));
    view.onDidDispose(() => {
      this.proxy.abortAll();
      this.view = undefined;
    });
  }

  /** Reveal the view and seed the command bar with text (investigate commands). */
  async seedPrompt(text: string): Promise<void> {
    await vscode.commands.executeCommand("kubeastra.chat.focus");
    this.post({ type: "prompt", text });
  }

  private async onMessage(msg: WebviewToHost): Promise<void> {
    switch (msg.type) {
      case "ready":
        await this.broadcastAuthState();
        return;
      case "api":
        await this.proxy.handle(msg, (m) => this.post(m));
        return;
      case "api-abort":
        this.proxy.abort(msg.id);
        return;
    }
  }

  private async broadcastAuthState(): Promise<void> {
    const base = this.auth.backendUrl();
    const signedIn = await this.auth.isSignedIn();
    this.post({
      type: "auth-state",
      signedIn,
      backendUrl: base,
      authRequired: base ? await this.auth.isAuthRequired(base) : false,
    });
  }

  private post(msg: HostToWebview): void {
    this.view?.webview.postMessage(msg);
  }

  private getHtml(webview: vscode.Webview): string {
    const dist = vscode.Uri.joinPath(this.ctx.extensionUri, "webview", "dist");
    const scriptUri = webview.asWebviewUri(vscode.Uri.joinPath(dist, "index.js"));
    const styleUri = webview.asWebviewUri(vscode.Uri.joinPath(dist, "index.css"));
    const nonce = makeNonce();
    // connect-src stays 'none': the webview never fetches the network itself —
    // all backend I/O goes through the host over postMessage.
    return /* html */ `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="Content-Security-Policy" content="
    default-src 'none';
    style-src ${webview.cspSource} 'unsafe-inline';
    script-src 'nonce-${nonce}';
    img-src ${webview.cspSource} https: data:;
    font-src ${webview.cspSource} https: data:;
  ">
  <link rel="stylesheet" href="${styleUri}">
</head>
<body>
  <div id="root"></div>
  <script type="module" nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
  }
}

function makeNonce(): string {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let out = "";
  for (let i = 0; i < 32; i++) out += chars[Math.floor(Math.random() * chars.length)];
  return out;
}
