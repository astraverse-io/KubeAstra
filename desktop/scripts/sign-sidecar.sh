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
# Runs from release-desktop.yml, after the sidecar is staged and before
# tauri-action starts. It is deliberately NOT a Tauri beforeBundleCommand:
# Tauri imports the signing certificate *after* running that hook, so the
# signer found no identity and skipped, and the build failed at notarization
# exactly as if the hook had never existed. The workflow signs first and owns
# the keychain setup itself.
#
# Signatures live inside the Mach-O, so they survive the bundler's copy into
# Contents/Resources/, and Tauri sealing the .app afterwards records them.
#
# Usage:  sign-sidecar.sh [--require-identity]
#
#   default             skip with a message when no identity exists
#   --require-identity  fail instead; for callers that know signing is on

set -euo pipefail

REQUIRE_IDENTITY=0
[ "${1:-}" = "--require-identity" ] && REQUIRE_IDENTITY=1

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
    if [ "$REQUIRE_IDENTITY" -eq 1 ]; then
        # The caller imported a certificate and expects it to be usable. A
        # silent skip here produces an app that builds, bundles, and is then
        # refused by Apple 90 seconds later with 192 errors — which is exactly
        # what happened when this path was reached by accident.
        echo "sign-sidecar: FAIL — signing was required but no Developer ID" >&2
        echo "              identity is visible. The keychain holding it is" >&2
        echo "              probably not in this shell's search list:" >&2
        security list-keychains -d user >&2
        exit 1
    fi
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
    # Anything inside a .framework is signed as part of that bundle, below.
    # Handing codesign a binary in a framework's interior fails with
    #   bundle format is ambiguous (could be app or framework)
    # because it tries to interpret the enclosing directory and cannot. That
    # is what broke the first run where signing actually happened.
    case "$f" in *.framework/*) continue ;; esac
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

# Frameworks are signed as bundles, at their version directory.
#
# The sidecar's Python.framework arrives FLATTENED: Python.framework/Python,
# Versions/Current/Python and Versions/3.x/Python are three byte-identical
# real files where a correct framework has the first two as symlinks. Signing
# the version directory then signs exactly one of the three, and Apple rejects
# the other two — which is precisely what it did, naming those two paths and
# nothing else.
#
# Relinking is the fix rather than signing each copy: codesign will not sign a
# binary inside a .framework as a loose file at all ("bundle format is
# ambiguous"), a framework with duplicated binaries is malformed by
# construction, and collapsing them reclaims ~11MB of the 16MB framework.
frameworks=0
while IFS= read -r -d '' fw; do
    version_dir="$(find "$fw/Versions" -maxdepth 1 -mindepth 1 -type d \
        ! -name Current 2>/dev/null | head -1 || true)"

    if [ -n "$version_dir" ]; then
        version="$(basename "$version_dir")"

        # Versions/Current -> <version>
        if [ ! -L "$fw/Versions/Current" ] && [ -e "$fw/Versions/Current" ]; then
            rm -rf "$fw/Versions/Current"
            ln -s "$version" "$fw/Versions/Current"
        fi

        # Top-level entries -> Versions/Current/<name>. Only when the copy is
        # byte-identical to what it should point at: anything else is a
        # framework this script does not understand, and deleting from it
        # would be worse than leaving it for Apple to reject.
        for entry in "$fw"/*; do
            name="$(basename "$entry")"
            [ "$name" = "Versions" ] && continue
            [ -L "$entry" ] && continue
            target="$version_dir/$name"
            [ -e "$target" ] || continue

            if [ -f "$entry" ] && [ -f "$target" ]; then
                [ "$(shasum -a 256 <"$entry")" = "$(shasum -a 256 <"$target")" ] \
                    || { echo "sign-sidecar: $name differs from $target, leaving it" >&2
                         continue; }
                # Delete rather than symlink. Tauri's `bundle.resources` glob
                # copies file-by-file and std::fs::copy follows a symlink, so a
                # top-level link is turned back into a plain copy inside the
                # .app — carrying the framework binary's embedded signature at
                # a path where nothing seals it. Apple calls that "the
                # signature of the binary is invalid", and it is the one error
                # that survived after everything else was signed.
                #
                # Directory symlinks (Resources, Versions/Current) do survive:
                # the glob does not recurse into them, which is why Apple
                # stopped reporting Versions/Current/Python once it became one.
                #
                # Nothing needs the top-level binary. Dependents resolve
                # @rpath/Python, and the sidecar was verified to start with
                # this file removed.
                rm -f "$entry"
                continue
            fi
            rm -rf "$entry"
            ln -s "Versions/Current/$name" "$entry"
        done
    fi

    # Sign the .framework itself, not the version directory inside it.
    #
    # Targeting the version directory was a workaround for the flattened
    # layout, which made the framework ambiguous to codesign. The relink above
    # removes that ambiguity, and signing a versioned framework at its bundle
    # root is the operation codesign is actually designed for: it signs the
    # current version and seals it so every path into it validates. Signing
    # the version directory instead produced a signature Apple called invalid
    # when reached through Python.framework/Python.
    codesign --force --timestamp --options runtime \
        --sign "$IDENTITY" "$fw"

    # Verify here rather than discovering it from Apple 90 seconds later. This
    # exact framework has now failed notarization twice, each time with an
    # error that codesign itself can see.
    if ! codesign --verify --strict --verbose=2 "$fw" 2>&1 | tail -3; then
        echo "sign-sidecar: FAIL — $fw does not verify after signing." >&2
        exit 1
    fi
    frameworks=$((frameworks + 1))
done < <(find "$SIDECAR" -type d -name "*.framework" -print0)

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

echo "sign-sidecar: signed $signed binaries and $frameworks framework(s)."

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
