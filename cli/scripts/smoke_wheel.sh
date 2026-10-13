#!/usr/bin/env bash
# Install a built kubeastra wheel the way a PyPI user does, and run it.
#
#   cli/scripts/smoke_wheel.sh <dist-dir>
#
# CI installs the CLI editable, so the code stays in the repo and the upgrade
# core is always found next to it — which is how 0.2.x passed CI while
# `kubeastra upgrade` failed for every PyPI user. This test refuses that
# shortcut: fresh venv, run from an empty directory outside any checkout,
# PYTHONPATH cleared. publish-cli.yml runs it before uploading anything.
set -euo pipefail

DIST="${1:?usage: smoke_wheel.sh <dist-dir>}"
PYTHON="${PYTHON:-python3}"

shopt -s nullglob
wheels=("$DIST"/kubeastra-*.whl)
[ "${#wheels[@]}" -eq 1 ] || { echo "smoke: expected exactly one wheel in $DIST, found ${#wheels[@]}" >&2; exit 1; }
WHEEL="$(cd "$(dirname "${wheels[0]}")" && pwd)/$(basename "${wheels[0]}")"
echo "smoke: $WHEEL"

# Fail fast with a precise message if the vendored core never made it in.
"$PYTHON" - "$WHEEL" <<'PY'
import sys, zipfile
names = set(zipfile.ZipFile(sys.argv[1]).namelist())
need = [
    "kubeastra/_vendor/services/upgrade/__init__.py",
    "kubeastra/_vendor/services/upgrade/maps.py",
    "kubeastra/_vendor/data/upgrades/api_deprecations.yaml",
]
missing = [n for n in need if n not in names]
if missing:
    sys.exit("smoke: wheel is missing the bundled upgrade core:\n  " + "\n  ".join(missing)
             + "\n(did the build skip cli/scripts/vendor_core.py?)")
print(f"smoke: wheel bundles the upgrade core ({len(names)} files)")
PY

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
"$PYTHON" -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install --quiet --disable-pip-version-check "$WHEEL"
KA="$WORK/venv/bin/kubeastra"

mkdir -p "$WORK/run/blocking" "$WORK/run/clean"
printf 'apiVersion: extensions/v1beta1\nkind: Ingress\nmetadata:\n  name: legacy\n' > "$WORK/run/blocking/ingress.yaml"
printf 'apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata:\n  name: current\n' > "$WORK/run/clean/ingress.yaml"
cd "$WORK/run"
unset PYTHONPATH

"$KA" --version

# A removed API: the gate must fail (exit 1) and name the API.
set +e
out="$("$KA" upgrade plan --target 1.30 --manifests blocking --fail-on-blocking 2>&1)"; rc=$?
set -e
echo "$out" | tail -n 4
if [ "$rc" -ne 1 ] || ! grep -q "extensions/v1beta1" <<<"$out"; then
  echo "smoke: FAIL — removed API should exit 1 and be named (exit $rc)" >&2; exit 1
fi

# Current APIs pass.
"$KA" upgrade plan --target 1.30 --manifests clean --fail-on-blocking >/dev/null

# SARIF carries the finding.
"$KA" upgrade plan --target 1.30 --manifests blocking -o sarif --no-fail-on-blocking > report.sarif
"$PYTHON" -c "import json,sys; r=json.load(open('report.sarif'))['runs'][0]['results']; sys.exit(0 if r else 'smoke: SARIF has no results')"

echo "smoke: OK — installed wheel runs \`kubeastra upgrade\` outside a checkout"
