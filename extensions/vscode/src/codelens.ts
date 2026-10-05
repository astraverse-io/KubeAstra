import * as vscode from "vscode";

/**
 * Shows an "Investigate with KubeAstra" code lens above every `kind:` line in a
 * YAML file — the top-level marker of a Kubernetes resource. Clicking it runs
 * `kubeastra.investigateFile`.
 */
export class KubeAstraCodeLensProvider implements vscode.CodeLensProvider {
  private static readonly KIND_RE = /^kind:\s*\S+/;

  provideCodeLenses(doc: vscode.TextDocument): vscode.CodeLens[] {
    const lenses: vscode.CodeLens[] = [];
    for (let line = 0; line < doc.lineCount; line++) {
      const text = doc.lineAt(line).text;
      if (KubeAstraCodeLensProvider.KIND_RE.test(text)) {
        lenses.push(
          new vscode.CodeLens(new vscode.Range(line, 0, line, text.length), {
            title: "$(search) Investigate with KubeAstra",
            command: "kubeastra.investigateFile",
          }),
        );
      }
    }
    return lenses;
  }
}
