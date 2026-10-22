# Resilience corpus sweep

Renders popular public Helm charts with **default values** (what you get from
`helm install <chart>` with no `-f`) and scores them with `kubeastra
resilience`. It exists to catch false positives before users do: a
high-severity finding on a well-maintained chart's defaults is either a real
problem in that chart or a bug in a rule, and either way someone should look.

```bash
scripts/resilience-corpus/sweep.sh out/                 # helm show + helm template, then score
RESCORE=1 scripts/resilience-corpus/sweep.sh out/       # re-score existing renders after a rule change
python3 scripts/resilience-corpus/summarize.py out/ --high
```

`helm template` only: nothing is installed anywhere. `out/versions.tsv` records
the exact chart versions rendered. Outputs are not committed.

## 2026-10-22 run

17 of 20 charts scored (`loki` 7.5.0 and `external-dns` 1.23.0 fail to render
with default values; the `sealed-secrets` repo URL now returns 404).
`cluster-autoscaler` 9.59.0 renders no workloads without a cloud provider set.

- **One high-severity finding, a true positive:** `podinfo` 6.15.0 runs one
  replica with `maxUnavailable: 1` (RES012), so a rollout may stop the only pod
  before its replacement is ready.
- **No chart's defaults block a node drain** (RES001: 0).
- **15 of the 16 charts that render workloads run a single replica behind a real Service with no PDB** (RES002) —
  every node drain is a short outage for that component until you raise replicas.
- **Rule fixed by this sweep:** a Service whose ports are all metrics ports
  (`http-metrics`, `metrics-port`, `http-monitoring`, …) used to count as
  "serving", so leader-elected controllers in cert-manager, kyverno and velero
  were flagged for RES002/RES006/RES008. Metrics-only Services no longer count
  (`is_metrics_only` in `mcp/services/resilience/model.py`); RES006 went from 7
  findings to 3, all on Services carrying real traffic (argo-cd dex, redis,
  applicationset webhook).

Numbers move as charts release. Re-run before quoting any of them.
