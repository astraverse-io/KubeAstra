#!/usr/bin/env bash
# Render every chart in charts.tsv with default values and score it with
# `kubeastra resilience`. Nothing is installed anywhere: `helm template` only.
#
#   scripts/resilience-corpus/sweep.sh OUT_DIR
#   RESCORE=1 scripts/resilience-corpus/sweep.sh OUT_DIR   # re-score existing renders, no network
#
# Writes OUT_DIR/<release>/{chart.yaml,rendered.yaml,report.json} and
# OUT_DIR/versions.tsv (the exact chart versions rendered, so a run can be
# reproduced). Then: python3 scripts/resilience-corpus/summarize.py OUT_DIR
# Needs helm and kubeastra (0.3.0+) on PATH, or PYTHONPATH=mcp from a checkout.
set -euo pipefail

OUT="${1:?usage: sweep.sh OUT_DIR}"
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$OUT"
[[ "${RESCORE:-}" == 1 ]] || : > "$OUT/versions.tsv"

if [[ "${RESCORE:-}" == 1 ]]; then
  for dir in "$OUT"/*/; do
    [[ -s "$dir/rendered.yaml" ]] || continue
    kubeastra resilience "$dir/rendered.yaml" -o json > "$dir/report.json" 2> "$dir/error.txt" || true
    echo "rescored $(basename "$dir")"
  done
  exit 0
fi

while IFS=$'\t' read -r release chart repo; do
  [[ -z "$release" || "$release" == \#* ]] && continue
  dir="$OUT/$release"
  mkdir -p "$dir"
  if [[ "$repo" == oci://* ]]; then src=("$repo/$chart"); else src=("$chart" --repo "$repo"); fi
  if ! helm show chart "${src[@]}" > "$dir/chart.yaml" 2> "$dir/error.txt"; then
    echo "skip $release: $(tail -1 "$dir/error.txt")"; continue
  fi
  version="$(awk '/^version:/ {print $2; exit}' "$dir/chart.yaml")"
  printf '%s\t%s\t%s\t%s\n' "$release" "$chart" "$version" "$repo" >> "$OUT/versions.tsv"
  if ! helm template "$release" "${src[@]}" --version "$version" > "$dir/rendered.yaml" 2> "$dir/error.txt"; then
    echo "skip $release $version: $(tail -1 "$dir/error.txt")"; continue
  fi
  # Exit code is not a failure here: no gate is set, so 0 = scored, 2 = no manifests.
  kubeastra resilience "$dir/rendered.yaml" -o json > "$dir/report.json" 2> "$dir/error.txt" || true
  echo "ok   $release $version"
done < "$HERE/charts.tsv"
