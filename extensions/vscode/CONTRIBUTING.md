# Contributing to KubeAstra for VS Code

## Architecture

The extension is two parts:

- **Extension host** (`src/`, bundled with esbuild): activation, cookie-based
  sign-in stored in VS Code SecretStorage, and an HTTP proxy that is the *only*
  thing that touches the network. It holds the session cookie and forwards every
  request the webview asks for, including streamed chat responses.
- **Webview** (`webview/`, a Vite React app): the Mission Control chat UI. It
  imports the Mission Control components **in place** from `ui/frontend/components`
  through a Vite alias, so the extension and the Next.js app render from a single
  source tree. The webview never fetches the backend directly; it messages the
  host.

Why the proxy: desktop-mode's security guard rejects a `vscode-webview://`
Origin, and carrying the server-mode HttpOnly cookie from inside a webview is
fragile. The host owns the credential instead.

## Develop

```bash
cd extensions/vscode
npm install
npm install --prefix webview
npm run build            # bundle the host
npm run build:webview    # bundle the webview
```

Then press **F5** in VS Code to launch an Extension Development Host.

Tests:

```bash
npm run typecheck
npm test --prefix webview   # webview unit tests (vitest)
npm test                    # activation test (@vscode/test-electron)
```

## Releasing

`.github/workflows/vscode-extension.yml` publishes to the VS Code Marketplace
and OpenVSX when a `vscode-v*` tag is pushed. Pushes to branches, PRs and manual
runs only build and test; the publish job is skipped for them.

1. Bump `version` in `package.json`. Nothing checks it against the tag, and the
   Marketplace rejects a version it has already published.
2. Merge, then tag the release commit and push the tag:

   ```bash
   git tag vscode-v0.2.0 && git push origin vscode-v0.2.0
   ```

Locally, `npm run package` produces `kubeastra-vscode.vsix` for testing.

### Publishing credentials

Marketplace auth uses **Microsoft Entra ID via GitHub OIDC**, not an Azure
DevOps PAT (Azure DevOps retires global PATs on 2026-12-01), so no Marketplace
token is stored. OpenVSX uses its own token. All of this is already set up; it
only matters if something is rotated or rebuilt.

- Entra app registration with a GitHub federated credential, subject
  `repo:astraverse-io/KubeAstra:environment:vscode-marketplace`. Its Client ID
  and Tenant ID are the `AZURE_CLIENT_ID` / `AZURE_TENANT_ID` secrets.
- The app's service principal is a **Contributor** member of the `astraverse-io`
  Marketplace publisher. Add it by its Azure DevOps profile ID, not the client
  or object ID. A publish that fails with
  `Access Denied: <id> needs the following permission(s)` prints that ID.
  Only a publisher **Owner** can add members.
- `OVSX_PAT` is an open-vsx.org token for the `astraverse-io` namespace.
- All three secrets live in the `vscode-marketplace` GitHub environment.
