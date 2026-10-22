# KubeAstra Resilience Pilot — GitHub Action

Score how well your workloads survive a node drain, a traffic spike or a
rollout, and fail the PR when they won't. **Keyless**: it scans rendered
manifests with KubeAstra's pure resilience core. No cluster, no API key.

It looks for the combinations that cause outages, not just single-object lint:
a PodDisruptionBudget that can never allow an eviction (so `kubectl drain`
stalls), an HPA scaling on CPU for a container that requests no CPU (so it
never scales), replicas with nothing spreading them across nodes, Services
sending traffic to containers with no readiness probe.

## Usage

```yaml
# .github/workflows/k8s-resilience.yml
name: K8s resilience
on: [pull_request]
jobs:
  resilience:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      # Render your manifests first (example: Helm) into ./rendered
      - run: helm template ./chart --output-dir ./rendered
      - uses: astraverse-io/KubeAstra/action/resilience@action-v1
        with:
          manifests: "./rendered"
          fail-on: "high"       # fail on any high-severity finding
          fail-under: "70"      # …or when the score drops below 70
```

### Code scanning (SARIF)

```yaml
      - uses: astraverse-io/KubeAstra/action/resilience@action-v1
        with:
          manifests: "./rendered"
          sarif-file: "kubeastra-resilience.sarif"
      - uses: github/codeql-action/upload-sarif@v3
        if: always()            # upload even when the gate failed
        with:
          sarif_file: "kubeastra-resilience.sarif"
```

Findings point at the file and line of the object in the rendered output.
Suppressed findings are uploaded as suppressed, so code scanning hides them.

### Badge

```yaml
      - uses: astraverse-io/KubeAstra/action/resilience@action-v1
        id: resilience
        with:
          manifests: "./rendered"
          badge-file: ".github/badges/resilience.json"
      # commit .github/badges/resilience.json on your default branch, then:
      # ![resilience](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/<owner>/<repo>/main/.github/badges/resilience.json)
      - run: echo "score ${{ steps.resilience.outputs.score }} (${{ steps.resilience.outputs.grade }})"
```

## Inputs

| Input | Default | Meaning |
|---|---|---|
| `manifests` | `.` | Rendered manifests: a file or directory. |
| `output` | `text` | `text`, `json`, or `sarif` to the step log. |
| `fail-under` | `""` | Fail when the score (0–100) is below this. |
| `fail-on` | `""` | Fail on any finding at or above `low`, `medium` or `high`. |
| `config` | `""` | Repo config path. Default: `.kubeastra/resilience.yaml` when present. |
| `sarif-file` | `""` | Also write SARIF here for `upload-sarif`. |
| `badge-file` | `""` | Also write a shields.io endpoint JSON here. |

Outputs: `score` (0–100, empty when there are no workloads) and `grade` (`A`–`F`, or `-`).

With neither `fail-under` nor `fail-on` set, the Action reports but never fails.
A directory with no Kubernetes manifests in it is an error, not a pass.

## Rules

| ID | Severity | What it catches |
|---|---|---|
| RES001 | high | PodDisruptionBudget can never allow an eviction (`minAvailable` ≥ replicas, or `maxUnavailable: 0`), counting the HPA floor |
| RES002 | medium | One replica behind a Service and no PodDisruptionBudget |
| RES003 | high | HPA scales on CPU/memory utilization a container doesn't request |
| RES004 | low | HPA `minReplicas` is 1 or unset |
| RES005 | medium | Replicas with no anti-affinity or topology spread |
| RES006 | medium | Service-backed container with no readinessProbe |
| RES007 | low | livenessProbe identical to readinessProbe |
| RES008 | low | Service-backed workload with no preStop hook |
| RES009 | medium | `terminationGracePeriodSeconds` doesn't outlast the preStop sleep |
| RES010 | low | Missing CPU/memory requests or memory limit |
| RES011 | low | Image untagged or `:latest` |
| RES012 | high | Rollout `maxUnavailable` can take every replica down |
| RES013 | medium | Service selector matches no workload (advisory when scanning files) |
| RES014 | low | PDB selector matches no workload (advisory when scanning files) |

Each serving workload (Deployment, StatefulSet, DaemonSet) starts at 100 and
loses 25 / 10 / 3 per high / medium / low finding. The score is the mean
across workloads.

## Turning things off

- **One object:** annotate it with `resilience.kubeastra.io/ignore: "RES005,RES008"` (or `"*"`).
- **The whole repo:** `.kubeastra/resilience.yaml`:

  ```yaml
  disable: [RES011]
  severity:
    RES004: medium
  ```

  An unknown rule ID or severity is an error, so a typo can't silently do nothing.

## Locally

```bash
pip install kubeastra            # 0.3.0 or newer
kubeastra resilience ./rendered --fail-on high
kubeastra resilience ./rendered --fix fixes/   # write patches, new PDBs and a README of what needs you
kubeastra resilience --live      # read the objects from your current kubectl context instead
```

`--fix` writes one strategic-merge patch per object (use it from a Kustomize
overlay, or `kubectl patch`), any new objects (PodDisruptionBudgets), and a
README listing what it couldn't decide for you — CPU requests, probe paths,
image tags, raising a singleton's replicas. It only ever writes a new or empty
directory, or one it wrote before. Objects rendered by Helm get the change as
advice for the chart's values, never a patch of the rendered output.
