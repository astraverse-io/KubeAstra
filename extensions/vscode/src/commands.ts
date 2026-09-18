import * as vscode from "vscode";
import { AuthManager } from "./auth";
import { KubeAstraChatViewProvider } from "./panel";

/** Registers all command-palette / context-menu commands. */
export function registerCommands(
  ctx: vscode.ExtensionContext,
  auth: AuthManager,
  chat: KubeAstraChatViewProvider,
): void {
  const reg = (id: string, fn: (...args: any[]) => any) =>
    ctx.subscriptions.push(vscode.commands.registerCommand(id, fn));

  reg("kubeastra.ask", async () => {
    await vscode.commands.executeCommand("kubeastra.chat.focus");
  });

  reg("kubeastra.signIn", () => auth.signIn());
  reg("kubeastra.signOut", () => auth.signOut());

  reg("kubeastra.connectCluster", async () => {
    await vscode.commands.executeCommand("kubeastra.chat.focus");
    await chat.seedPrompt("Connect to my cluster and show its status.");
  });

  reg("kubeastra.investigateFile", async () => {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
      vscode.window.showWarningMessage("KubeAstra: no active file to investigate.");
      return;
    }
    const name = editor.document.fileName.split("/").pop() ?? "this manifest";
    await chat.seedPrompt(
      `Investigate ${name}:\n\n\`\`\`yaml\n${editor.document.getText()}\n\`\`\``,
    );
  });

  reg("kubeastra.investigateSelection", async () => {
    const editor = vscode.window.activeTextEditor;
    const text = editor?.document.getText(editor.selection);
    if (!text) {
      vscode.window.showWarningMessage("KubeAstra: nothing selected.");
      return;
    }
    await chat.seedPrompt(`Investigate this:\n\n\`\`\`\n${text}\n\`\`\``);
  });
}
