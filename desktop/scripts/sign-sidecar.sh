#!/usr/bin/env bash
#
# Code-sign every Mach-O binary inside the PyInstaller sidecar.
#
# Notarization rejects an app unless EVERY executable in it is individually
# signed with a Developer ID certificate, a secure timestamp, and the hardened
# runtime. Tauri signs the .app bundle and its own executable — but the sidecar
# arrives through `bundle.resources`, and codesign does not recurse into
# resources. It never has; --deep does not fix it either, and Apple documents
# --deep as the wrong tool for this.
#
# The result is a build that succeeds locally, produces a DMG, and is refused
# by Apple with one error per file. The first attempt (desktop-v0.2.0,
# 2026-08-11) came back with 192 errors across 96 binaries, all of them under
# Contents/Resources/binaries/kubeastra-backend/ and none of them in the app:
#
#   "The binary is not signed with a valid Developer ID certificate."   x186
#   "The signature does not include a secure timestamp."                x192
#   "The signature of the binary is invalid."                             x6
#   "The executable does not have the hardened runtime enabled."          x2
#
# PyInstaller leaves ad-hoc signatures on these files, which is why --force is
# required: without it codesign refuses to replace what is already there.
#
# Runs from tauri.conf.json's beforeBundleCommand, i.e. after the Rust build
# and before the bundler copies these files into the .app. Signatures live
# inside the Mach-O, so they survive that copy.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SIDECAR="$ROOT/desktop/src-tauri/binaries/kubeastra-backend"
ENTITLEMENTS="$ROOT/desktop/src-tauri/entitlements.plist"

[ "$(uname -s)" = "Darwin" ] || { echo "sign-sidecar: not macOS, nothing to do."; exit 0; }

if [ ! -d "$SIDECAR" ]; then
    echo "sign-sidecar: no sidecar at $SIDECAR — did PyInstaller run?" >&2
    exit 1
fi

# tauri-action exports this after importing APPLE_CERTIFICATE. Falling back to
# the keychain keeps a local signed build working without setting it by hand.
IDENTITY="${APPLE_SIGNING_IDENTITY:-}"
if [ -z "$IDENTITY" ]; then
    # `|| true` is load-bearing. With `set -e` and `pipefail`, grep finding
    # nothing makes the whole substitution fail and kills the script here —
    # silently, before it can report that there is no identity. Having no
    # identity is a normal outcome, not an error.
    IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
        | grep "Developer ID Application" | head -1 \
        | sed -n 's/.*"\(.*\)"/\1/p' || true)"
fi

if [ -z "$IDENTITY" ]; then
    # An unsigned build is a legitimate outcome — it is what the workflow
    # produces when no certificate secret exists. Failing here would break it.
    echo "sign-sidecar: no Developer ID identity available; leaving the sidecar unsigned."
    exit 0
fi

echo "sign-sidecar: signing with '$IDENTITY'"

# Only real files. Apple reports symlinks (Python.framework/Python,
# Versions/Current/Python) as separate errors, but signing the file they
# resolve to clears all of them — and codesign on a symlink fails.
mach_o=()
while IFS= read -r -d '' f; do
    if file -b "$f" 2>/dev/null | grep -q "Mach-O"; then
        mach_o+=("$f")
    fi
done < <(find "$SIDECAR" -type f -print0)

if [ ${#mach_o[@]} -eq 0 ]; then
    echo "sign-sidecar: found no Mach-O binaries under $SIDECAR." >&2
    exit 1
fi

# Deepest first. Signing a container seals the hashes of what it holds, so a
# nested binary signed afterwards invalidates the seal above it.
IFS=$'\n' sorted=($(printf '%s\n' "${mach_o[@]}" \
    | awk -F/ '{print NF"\t"$0}' | sort -rn | cut -f2-))
unset IFS

signed=0
for f in "${sorted[@]}"; do
    codesign --force --timestamp --options runtime \
        --sign "$IDENTITY" "$f" >/dev/null 2>&1 \
        || { echo "sign-sidecar: FAILED on $f" >&2; codesign --force --timestamp \
             --options runtime --sign "$IDENTITY" "$f"; }
    signed=$((signed + 1))
done

# The sidecar's own entry point is the one that actually executes, so it needs
# the entitlements. The rest are loaded into its process and inherit them.
codesign --force --timestamp --options runtime \
    --entitlements "$ENTITLEMENTS" \
    --sign "$IDENTITY" "$SIDECAR/kubeastra-backend"

echo "sign-sidecar: signed $signed binaries."

# Verify rather than trust the exit code — this repo has been bitten by tools
# that exit 0 on a broken bundle.
codesign --verify --deep --strict --verbose=2 "$SIDECAR/kubeastra-backend" 2>&1 | tail -2

if codesign -dv --verbose=4 "$SIDECAR/kubeastra-backend" 2>&1 | grep -q "Timestamp="; then
    echo "sign-sidecar: OK — entry point carries a secure timestamp."
else
    echo "sign-sidecar: FAIL — no secure timestamp on the entry point." >&2
    echo "              Notarization will reject this. Usually a network" >&2
    echo "              failure reaching Apple's timestamp server." >&2
    exit 1
fi
