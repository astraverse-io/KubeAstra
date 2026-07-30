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
  - **Linux**: `libgtk-3-dev`, `libwebkit2gtk-4.1-dev`, `libappindicator3-dev`, `librsvg2-dev`, `libsecret-1-dev`
  - **Windows**: C++ Build Tools

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
