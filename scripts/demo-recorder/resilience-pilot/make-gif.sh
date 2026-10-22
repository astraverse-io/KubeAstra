#!/usr/bin/env bash
# Rebuild docs/media/resilience-pilot-demo.gif from a fresh, real run of the
# published CLI.
#
#   scripts/demo-recorder/resilience-pilot/make-gif.sh [kubeastra-version]
#
# Same requirements and pipeline as ../upgrade-pilot/make-gif.sh (Python 3.11+,
# Node 20+, `npm install` in scripts/demo-recorder, Google Chrome), and it
# reuses that demo's renderer (frames.mjs) and assembler (assemble.py).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
SHARED="$HERE/../upgrade-pilot"
VERSION="${1:-}"
PKG="kubeastra${VERSION:+==$VERSION}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "kubeastra needs Python 3.11+; set PYTHON=/path/to/python3.11+")'
"$PYTHON" -m venv "$TMP/venv"
"$TMP/venv/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir "$PKG" Pillow

# Outside the checkout, PYTHONPATH unset: what a `pip install kubeastra` user gets.
mkdir -p "$TMP/work"
cp -R "$HERE/fixtures/rendered" "$TMP/work/rendered"
(cd "$TMP" && env -u PYTHONPATH "$TMP/venv/bin/python" "$HERE/capture.py" "$TMP/work" "$HERE/timeline.json")

(cd "$HERE/.." && node upgrade-pilot/frames.mjs "$HERE/timeline.json" "$TMP/frames" "$HERE/player.html")
"$TMP/venv/bin/python" "$SHARED/assemble.py" "$TMP/frames" "$REPO/docs/media/resilience-pilot-demo.gif"
