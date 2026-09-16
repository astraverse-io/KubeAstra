import * as vscode from "vscode";
import { AuthManager } from "./auth";
import { ApiProxy } from "./apiProxy";
import { KubeAstraChatViewProvider } from "./panel";
import { registerCommands } from "./commands";
import { KubeAstraCodeLensProvider } from "./codelens";
import { registerStatusBar } from "./statusBar";

export function activate(context: vscode.ExtensionContext): void {
  const auth = new AuthManager(context);
  const proxy = new ApiProxy(auth);
  const chat = new KubeAstraChatViewProvider(context, auth, proxy);

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(KubeAstraChatViewProvider.viewType, chat, {
      webviewOptions: { retainContextWhenHidden: true },
    }),
  );

  registerCommands(context, auth, chat);
  registerStatusBar(context, auth);

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
