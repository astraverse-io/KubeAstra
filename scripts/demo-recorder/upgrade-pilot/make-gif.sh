#!/usr/bin/env bash
# Rebuild docs/media/upgrade-pilot-demo.gif from a fresh, real run of the
# published CLI.
#
#   scripts/demo-recorder/upgrade-pilot/make-gif.sh [kubeastra-version]
#
# Needs: Python 3.11+ (set PYTHON=... if `python3` is older), Node 20+,
# `npm install` done in scripts/demo-recorder, and Google Chrome (Playwright
# drives it). Installs kubeastra from PyPI and Pillow into a throwaway venv;
# nothing is installed globally.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
VERSION="${1:-}"
PKG="kubeastra${VERSION:+==$VERSION}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "kubeastra needs Python 3.11+; set PYTHON=/path/to/python3.11+")'
"$PYTHON" -m venv "$TMP/venv"
"$TMP/venv/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir "$PKG" Pillow

# Run outside the checkout, with PYTHONPATH unset, so the recording shows what a
# `pip install kubeastra` user gets (capture.py refuses anything else).
mkdir -p "$TMP/work"
cp -R "$HERE/fixtures/rendered" "$TMP/work/rendered"
(cd "$TMP" && env -u PYTHONPATH "$TMP/venv/bin/python" "$HERE/capture.py" "$TMP/work" "$HERE/timeline.json")

(cd "$HERE/.." && node upgrade-pilot/frames.mjs "$HERE/timeline.json" "$TMP/frames")
"$TMP/venv/bin/python" "$HERE/assemble.py" "$TMP/frames" "$REPO/docs/media/upgrade-pilot-demo.gif"
