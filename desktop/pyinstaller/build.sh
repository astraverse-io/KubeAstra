#!/usr/bin/env bash
#
# Build the frozen backend, and fail loudly when the result is broken.
#
# PyInstaller exits 0 even when hidden imports fail to resolve, so a plain
# `pyinstaller ...` in CI goes green while producing a binary that crashes on
# launch. That happened twice during Phase 2. Every check below exists because
# something got past the previous set.
#
# Usage:  desktop/pyinstaller/build.sh [dist_dir] [work_dir]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SPEC="$REPO_ROOT/desktop/pyinstaller/kubeastra-backend.spec"
DIST="${1:-$REPO_ROOT/desktop/build/dist}"
WORK="${2:-$REPO_ROOT/desktop/build/work}"
OUT="$DIST/kubeastra-backend"
LOG="$WORK/pyinstaller.log"

mkdir -p "$WORK"
rm -rf "$OUT"

echo "==> Building $SPEC"
set +e
pyinstaller "$SPEC" --distpath "$DIST" --workpath "$WORK" --noconfirm 2>&1 | tee "$LOG"
status=${PIPESTATUS[0]}
set -e
[ "$status" -eq 0 ] || { echo "FAIL: pyinstaller exited $status"; exit 1; }

fail() { echo "FAIL: $1"; exit 1; }

# 1. Unresolved hidden imports. PyInstaller only warns; a missing provider or
#    uvicorn protocol module then surfaces as a crash on the user's machine.
if grep -q "ERROR: Hidden import" "$LOG"; then
    echo "--- unresolved hidden imports ---"
    grep "ERROR: Hidden import" "$LOG" | sed 's/^[0-9]* //'
    fail "hidden imports did not resolve; fix the names in the spec"
fi

# 2. onedir layout. The LGPL-2.1 compliance argument for bundling paramiko
#    depends on modules staying separate, replaceable files (DESKTOP_APP_PLAN
#    "never switch to onefile without revisiting this"), and Tauri ships this
#    as a resource directory.
[ -d "$OUT" ]            || fail "expected a onedir tree at $OUT"
[ -d "$OUT/_internal" ]  || fail "no _internal/ — the onedir layout is broken"
[ -x "$OUT/kubeastra-backend" ] || fail "no executable at $OUT/kubeastra-backend"

# 3. No developer files. A bare datas=[(MCP_DIR, 'mcp')] once swept the
#    gitignored mcp/.env into a shipped .dmg — and settings.py reads that file,
#    so those values would have applied on every user's machine.
leaked="$(find "$OUT" -name '.env*' -o -name '.git*' -o -name '.DS_Store' | head -20)"
[ -z "$leaked" ] || { echo "$leaked"; fail "developer dotfiles are in the bundle"; }

for unwanted in _internal/mcp/tests _internal/mcp/scripts; do
    [ ! -e "$OUT/$unwanted" ] || fail "$unwanted should not ship to users"
done
if find "$OUT" -type d -name '__pycache__' | grep -q .; then
    fail "__pycache__ directories are in the bundle"
fi

# 4. It has to actually start. Everything above can pass on a binary that dies
#    immediately — which is exactly what shipped before.
echo "==> Smoke test: launching the frozen backend"
python3 - "$OUT/kubeastra-backend" <<'PY'
import subprocess, sys, urllib.request

proc = subprocess.Popen([sys.argv[1]], stdout=subprocess.PIPE, text=True)
port = None
ready = False
try:
    for line in proc.stdout:            # drain continuously; never break early
        print("   ", line.rstrip())
        if line.startswith("PORT="):
            port = line[5:].strip()
        elif line.strip() == "READY":
            ready = True
            break
    if not (port and ready):
        sys.exit("FAIL: backend never reached READY")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=30) as r:
        if r.status != 200:
            sys.exit(f"FAIL: /health returned {r.status}")
    print(f"    /health OK on port {port}")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
PY

echo "==> OK: $OUT ($(du -sh "$OUT" | cut -f1))"
