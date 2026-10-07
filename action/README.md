# KubeAstra Upgrade Pilot — GitHub Action

Fail a PR when its manifests use Kubernetes APIs that are **removed** at a target
version, and print the ordered migration plan. **Keyless** — it scans rendered
manifests (no cluster, no API key) using KubeAstra's pure planner core.

## Usage

```yaml
# .github/workflows/k8s-upgrade-check.yml
name: K8s upgrade check
on: [pull_request]
jobs:
  upgrade-pilot:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      # Render your manifests first (example: Helm) into ./rendered
      - run: helm template ./chart --output-dir ./rendered
      - uses: astraverse-io/kubeastra/action@v1
        with:
          target: "1.31"
          manifests: "./rendered"
          output: "text"        # text | json | sarif
          fail-on-blocking: "true"
```

### Code scanning (SARIF)

Emit a SARIF report and upload it so findings show in the **Security → Code
scanning** tab:

```yaml
      - uses: astraverse-io/kubeastra/action@v1
        with:
          target: "1.31"
          manifests: "./rendered"
          sarif-file: "kubeastra-upgrade.sarif"
          fail-on-blocking: "false"   # let code scanning surface them instead of failing
      - uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: "kubeastra-upgrade.sarif"
```

## Inputs

| Input | Default | Meaning |
|---|---|---|
| `target` | — (required) | Target Kubernetes minor, e.g. `1.31`. |
| `manifests` | `.` | Directory of rendered manifests to scan. |
| `output` | `text` | `text`, `json`, or `sarif` to the step log. |
| `sarif-file` | `""` | Also write SARIF here for `upload-sarif`. |
| `fail-on-blocking` | `true` | Exit non-zero when a removed API is in use. |

## Notes

- **Static-mode fidelity:** scanning rendered manifests is authoritative for the
  API-deprecation check (what a PR gate wants). Operator-compatibility advice
  needs a live cluster and is skipped in this mode.
- The same check runs locally: `kubeastra upgrade plan --target 1.31 --manifests ./rendered`.
