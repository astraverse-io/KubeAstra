# KubeAstra for VS Code

AI-powered Kubernetes investigation, inside your editor. Ask why a pod is
crashing, investigate a manifest, and review proposed fixes — powered by a
KubeAstra backend you run yourself.

> **Preview.** This extension talks to a **self-hosted KubeAstra backend**
> (server mode). There is no hosted service. Point it at your deployment with the
> `kubeastra.backendUrl` setting.

## Architecture

The extension is two parts:

- **Extension host** (`src/`, bundled with esbuild) — activation, cookie-based
  sign-in stored in VS Code SecretStorage, and an HTTP proxy that is the *only*
  thing that touches the network. It holds the session cookie and forwards every
  request the webview asks for, including streamed chat responses.
- **Webview** (`webview/`, a Vite React app) — the Mission Control chat UI. It
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

## Use it

1. Install (once published): search **KubeAstra** in the Extensions view, or on
   OpenVSX.
2. Run **KubeAstra: Sign in** from the Command Palette and enter your backend
   URL + credentials.
3. Open the **KubeAstra** view in the activity bar to chat. Kubernetes YAML
   manifests show an inline *Investigate* code lens, and you can right-click any
   YAML, Terraform, or HCL file → **Investigate with KubeAstra** from the
   context menu.

## Settings

- `kubeastra.backendUrl` — base URL of your self-hosted backend (e.g.
  `http://localhost:8000`). No default; you are prompted on first sign-in.
- `kubeastra.autoSignIn` — prompt to sign in on activation when a backend is set.

## Publishing (maintainers)

Locally, `npm run package` produces `kubeastra-vscode.vsix`. Tag
`vscode-v<version>` (e.g. `vscode-v0.1.0`) to trigger
`.github/workflows/vscode-extension.yml`, which packages the `.vsix` and
publishes to the VS Code Marketplace and OpenVSX.

Marketplace auth uses **Microsoft Entra ID via GitHub OIDC**, not an Azure
DevOps PAT (Azure DevOps retires global PATs on 2026-12-01), so no Marketplace
token is ever stored. OpenVSX still uses its own token.

**Before the first publish:**

- [ ] Create the `astraverse-io` publisher at
      https://marketplace.visualstudio.com/manage
- [ ] Create an Entra app registration with a GitHub federated credential
      (subject `repo:astraverse-io/KubeAstra:environment:vscode-marketplace`);
      record its Client ID / Tenant ID → repo secrets `AZURE_CLIENT_ID` /
      `AZURE_TENANT_ID`.
- [ ] Add that app's service principal as a **Contributor** member of the
      Marketplace publisher.
- [ ] Create the `astraverse-io` open-vsx.org namespace + a token → repo secret
      `OVSX_PAT`.
- [ ] Add the three secrets to the `vscode-marketplace` GitHub environment.
- [ ] Capture 2–3 screenshots (and ideally a short GIF) of the chat, the
      diagnosis card, and the YAML code lens; add them under `resources/` and
      reference them here — a UI extension's listing needs them.
- [ ] Bump `version` in `package.json` to match the `vscode-v<version>` tag.

## Status

Shipped M1–M5: activation (including on any YAML file), the Mission Control chat
webview reusing `ui/frontend/components`, cookie sign-in, streaming chat, YAML
code lens / context menu, live cluster status in the header and status bar, and
Marketplace/OpenVSX publish CI. Published as **Preview**.
