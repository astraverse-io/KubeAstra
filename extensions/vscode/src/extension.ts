import * as vscode from "vscode";
import { AuthManager } from "./auth";
import { ApiProxy } from "./apiProxy";
import { ClusterMonitor } from "./cluster";
import { KubeAstraChatViewProvider } from "./panel";
import { registerCommands } from "./commands";
import { KubeAstraCodeLensProvider } from "./codelens";
import { registerStatusBar } from "./statusBar";

export function activate(context: vscode.ExtensionContext): void {
  const auth = new AuthManager(context);
  const proxy = new ApiProxy(auth);
  const cluster = new ClusterMonitor(auth);
  context.subscriptions.push(cluster);
  const chat = new KubeAstraChatViewProvider(context, auth, proxy, cluster);

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(KubeAstraChatViewProvider.viewType, chat, {
      webviewOptions: { retainContextWhenHidden: true },
    }),
  );

  registerCommands(context, auth, chat);
  registerStatusBar(context, auth, cluster);

  // Re-check cluster reachability whenever auth changes, then poll.
  context.subscriptions.push(auth.onChange(() => void cluster.refresh()));
  cluster.start();

  context.subscriptions.push(
    vscode.languages.registerCodeLensProvider(
      [{ language: "yaml" }],
      new KubeAstraCodeLensProvider(),
    ),
  );

  // Optional first-run sign-in prompt when a backend is already configured.
  if (
    auth.backendUrl() &&
    vscode.workspace.getConfiguration("kubeastra").get<boolean>("autoSignIn")
  ) {
    void (async () => {
      if (!(await auth.isSignedIn())) {
        const pick = await vscode.window.showInformationMessage(
          "Sign in to KubeAstra?",
          "Sign in",
        );
        if (pick === "Sign in") await auth.signIn();
      }
    })();
  }
}

export function deactivate(): void {}
