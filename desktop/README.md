# KubeAstra Desktop

Native desktop wrapper for KubeAstra built with Tauri v2 and PyInstaller.

## Overview

KubeAstra Desktop runs as a local native application. The backend runs as a frozen Python sidecar executable (using embedded Qdrant local storage and OS Keychain secrets), while the frontend runs in a native Tauri webview.

## Architecture

- `src-tauri/`: Tauri v2 application shell (Rust).
- `pyinstaller/`: PyInstaller specification (`kubeastra-backend.spec`) and build hooks.
- `requirements.txt`: Lightweight Python dependencies for desktop mode (no PyTorch, no sentence-transformers).

## Prerequisites

- Node.js 20+
- Python 3.11+
- Rust toolchain (`cargo`, `rustc`)
- Platform tools:
  - **macOS**: Xcode Command Line Tools
  - **Windows**: C++ Build Tools

## Platforms

**macOS and Windows only.** There is no Linux desktop build, and this is a
deliberate reversal of the original plan — which chose Linux for the first
release because it needed no code-signing certificate, not because users
wanted it.

Every native feature degrades on Linux:

| | |
|---|---|
| Tray icon | Needs `libappindicator`, and GNOME does not show tray icons without a user-installed extension |
| Global shortcut | Wayland blocks global shortcuts by design, and it is the default on Ubuntu, Fedora and GNOME |
| Keychain | Needs SecretService; otherwise falls back to a `0600` file |
| Webview | WebKitGTK version fragmentation — the same binary breaks across distros |

**Linux users are already served.** `kubeastra open` runs the app in a browser
on Linux today, and server mode (Helm, docker-compose) is Linux-native and
unaffected. That covers the cases where Linux actually matters for KubeAstra;
a desktop installer would have been the weakest build on the hardest platform
to verify.

Revisit if there is real demand. The Tauri config would need `appimage`/`deb`
back in `bundle.targets`, a `ubuntu-*` matrix entry with the GTK/WebKit dev
packages, and — most of the cost — a bundle-verification step per distro.

## Development Setup

1. **Install dependencies**:
   ```bash
   # Install frontend dependencies
   npm ci

   # Install desktop Python environment
   python -m venv venv
   source venv/bin/activate
   pip install -r desktop/requirements.txt
   ```

2. **Build frontend static SPA export**:
   ```bash
   npm run build:desktop --prefix ui/frontend
   ```

3. **Run Tauri Dev Server**:
   ```bash
   cd desktop
   cargo tauri dev
   ```

## Production Packaging

1. **Build and verify the backend**:

   ```bash
   desktop/pyinstaller/build.sh
   ```

   Always use this script rather than calling `pyinstaller` directly.
   PyInstaller exits 0 when hidden imports fail to resolve, so a bare
   invocation reports success while producing a binary that crashes on
   launch. The script adds the checks it does not perform itself: unresolved
   hidden imports, the onedir layout, no developer dotfiles in the output, and
   a real launch plus `GET /health`.

   Output goes to `desktop/build/dist/kubeastra-backend/` (gitignored).

2. **Stage the sidecar**:

   ```bash
   rm -rf desktop/src-tauri/binaries
   mkdir -p desktop/src-tauri/binaries
   cp -r desktop/build/dist/kubeastra-backend desktop/src-tauri/binaries/kubeastra-backend
   ```

   No target-triple suffix. The tree ships through `bundle.resources`, not
   `externalBin` — see below.

3. **Build the installers**:

   ```bash
   cd desktop
   cargo tauri build
   ```

4. **Verify the bundle, not just the build.** `cargo tauri build` exits 0 on
   bundles whose sidecar cannot start:

   ```bash
   APP=desktop/src-tauri/target/*/release/bundle/macos/KubeAstra.app
   $APP/Contents/Resources/binaries/kubeastra-backend/kubeastra-backend
   ```

   It must print `PORT=`, `URL=` and `READY`. CI does this automatically.

## How the shell talks to the app

The tray, the global shortcut (`Cmd/Ctrl+Shift+K`) and `kubeastra://` deep
links all steer the webview the same way: by navigating to
`#kubeastra=<action>&…`. A small client component, `DesktopBridge`, listens for
`hashchange` and acts.

**Not Tauri IPC**, for two reasons:

1. The app is served from `http://127.0.0.1:<port>` — a remote origin, which
   Tauri v2 only exposes IPC to after explicitly opting that domain in.
2. A fragment-only change does not reload the page. Pressing the shortcut
   during an investigation must not discard it.

The splash screen's failure path (`#fail=`) uses the same channel.

Actions:

| Fragment | Sent by | Effect |
|---|---|---|
| `#kubeastra=focus` | shortcut, tray "New investigation…" | Front the window, focus the input |
| `#kubeastra=investigate&ns=…&pod=…` | `kubeastra://investigate?ns=…&pod=…` | Front the window, submit an investigation |

Every value is percent-encoded by the Rust side and re-parsed with
`URLSearchParams`, so a pod name containing `&` or `=` cannot forge extra
parameters — there is a test for exactly that. A request that arrives before
the backend is ready is queued and replayed, which is the normal case when a
deep link launches the app cold.

The tray icon is `icons/tray.png`, **not** `icons/icon.png`. macOS renders
template icons from the alpha channel alone, and `icon.png` is 100% opaque —
using it puts a solid black square in the menu bar.

## Why resources instead of externalBin

The backend is a PyInstaller **onedir** build — a launcher plus an
`_internal/` tree it cannot start without. onedir is required: the LGPL-2.1
compliance argument for bundling `paramiko` depends on modules staying
separate, replaceable files (`DESKTOP_APP_PLAN.md`: *"never switch to onefile
without revisiting this"*).

Tauri's `externalBin` expects a single self-contained executable and flattens
whatever it is given into `Contents/MacOS/`. Pointed at a onedir tree it
produced a bundle that built cleanly and exited 0, then failed at launch:

```
Failed to load Python shared library '.../Contents/Frameworks/Python'
```

`_internal/` no longer existed. The app opened to a blank window with the
error only on stderr. `bundle.resources` preserves the directory.

## Two rules for the spec

The PyInstaller spec collects `mcp/` **file by file, through an allow-list**.
Never add a bare directory entry such as `datas=[(MCP_DIR, 'mcp')]`. A build
using that form swept the developer's gitignored `mcp/.env` into a shipped
`.dmg` — and since `config/settings.py` reads `_PROJECT_ROOT / ".env"`, those
values would have been applied on every user's machine.

`upx` stays off. It rewrites Mach-O headers, invalidating code signatures and
breaking notarization.
