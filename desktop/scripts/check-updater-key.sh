#!/usr/bin/env bash
#
# Refuse to build a release whose updater is signed by the development key.
#
# Phase 2 already shipped an updater config once. It was removed rather than
# fixed, because it carried a placeholder public key and the plugin was not
# even a dependency — nothing failed, nothing warned, and the feature simply
# did not exist while appearing to. This script is why that cannot recur.
#
# The failure it prevents is unusually expensive. The updater public key is
# baked into every installed copy, and an installed app will only accept
# updates signed by the matching private key. Ship one release signed by the
# dev key and every user who installs it is permanently unreachable by
# auto-update — the fix is "download the app again manually", and you cannot
# push that instruction through the channel that is broken.
#
# Usage:  desktop/scripts/check-updater-key.sh [--release]
#
#   default    warn only; fine for local builds
#   --release  fail on the dev key; CI uses this on tagged builds

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONF="$ROOT/desktop/src-tauri/tauri.conf.json"

# sha256 of the development public key generated 2026-07-30. Storing the hash
# rather than the key keeps a second copy of it out of the repo, and makes the
# check work no matter how the key is formatted or wrapped.
DEV_KEY_SHA256="48696364d81c28240c67206c32378137ee93df120ca86e18c77a3cf3366565bf"

RELEASE=0
[ "${1:-}" = "--release" ] && RELEASE=1

# `python` on the Windows runner, `python3` on macOS. setup-python provides
# whichever the platform calls it, and this script now runs on both lanes.
PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python

pubkey="$("$PY" -c "
import json, sys
conf = json.load(open('$CONF'))
print((conf.get('plugins', {}).get('updater', {}) or {}).get('pubkey', ''))
")"

if [ -z "$pubkey" ]; then
    echo "FAIL: no updater pubkey in tauri.conf.json."
    echo "      An app built without one cannot verify updates at all."
    exit 1
fi

# shasum is Perl and is not guaranteed in Git Bash on the Windows runner;
# sha256sum is. Try both rather than assume the macOS spelling.
if command -v shasum >/dev/null 2>&1; then
    actual="$(printf '%s' "$pubkey" | shasum -a 256 | cut -d' ' -f1)"
else
    actual="$(printf '%s' "$pubkey" | sha256sum | cut -d' ' -f1)"
fi

if [ "$actual" = "$DEV_KEY_SHA256" ]; then
    if [ "$RELEASE" -eq 1 ]; then
        cat >&2 <<'EOF'
FAIL: this release would be signed by the DEVELOPMENT updater key.

Every copy installed from it would only ever accept updates signed by a
private key that lives in a scratch directory — which means, in practice,
that auto-update is permanently broken for those users and the only remedy
is asking them to re-download by hand.

To fix:
  1. Generate the production keypair, once:
         cargo tauri signer generate -w ~/.tauri/kubeastra.key
  2. Put the PUBLIC key in tauri.conf.json under plugins.updater.pubkey
  3. Put the PRIVATE key and its password in GitHub Actions secrets as
         TAURI_SIGNING_PRIVATE_KEY
         TAURI_SIGNING_PRIVATE_KEY_PASSWORD
  4. Keep an OFFLINE backup of the private key. Losing it permanently ends
     auto-update for every installed copy — treat it like the signing certs.
  5. Leave DEV_KEY_SHA256 alone. It is the hash of the key that must never
     ship, not the hash of the current one — setting it to the production
     key would make this guard reject every real release. Change it only if
     the development keypair itself is regenerated.
EOF
        exit 1
    fi
    echo "warning: updater is using the DEVELOPMENT key — fine locally, fatal for a release."
    echo "         a release build will be rejected until the production key is in place."
    exit 0
fi

echo "OK: updater pubkey is not the development key (sha256 ${actual:0:12}…)"
