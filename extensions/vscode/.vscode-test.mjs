import { defineConfig } from "@vscode/test-cli";

// Runs the compiled Mocha tests (out-test/) against a downloaded VS Code.
// The extension bundle (out/extension.js) and webview (webview/dist) are built
// by the `pretest` script before this runs.
export default defineConfig({
  files: "out-test/test/**/*.test.js",
  version: "stable",
});
