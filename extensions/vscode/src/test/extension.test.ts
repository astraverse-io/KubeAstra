import * as assert from "assert";
import * as vscode from "vscode";

const EXT_ID = "astraverse-io.kubeastra-vscode";

suite("KubeAstra extension", () => {
  test("activates and registers its commands", async () => {
    const ext = vscode.extensions.getExtension(EXT_ID);
    assert.ok(ext, "extension is installed");
    await ext!.activate();

    const commands = await vscode.commands.getCommands(true);
    for (const id of ["kubeastra.ask", "kubeastra.investigateFile", "kubeastra.signIn"]) {
      assert.ok(commands.includes(id), `command ${id} registered`);
    }
  });

  test("shows an Investigate code lens on a YAML manifest", async () => {
    const doc = await vscode.workspace.openTextDocument({
      content: "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\n",
      language: "yaml",
    });
    await vscode.window.showTextDocument(doc);

    const lenses = await vscode.commands.executeCommand<vscode.CodeLens[]>(
      "vscode.executeCodeLensProvider",
      doc.uri,
    );
    assert.ok(lenses && lenses.length > 0, "at least one code lens");
    assert.strictEqual(lenses![0].command?.command, "kubeastra.investigateFile");
  });
});
