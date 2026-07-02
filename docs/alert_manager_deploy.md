# Alert Manager — Deployment & Operations

Practical guide to deploying the alert-manager subsystem (merged into the
K8s DevOps Assistant) in a real cluster, wiring Alertmanager to it, and
scraping its metrics.

This guide assumes you have already deployed the assistant Helm chart at least
once. It does **not** re-document everything in `helm/kubeastra/`;
it covers only what's specific to alert ingestion and incident memory.

---

## 1. Webhook secret

The webhook (`POST /api/v1/alerts/webhook`) is exempt from interactive
user-session auth — Alertmanager has no user session to present. It's instead
gated by a shared bearer token, surfaced into the backend pod via the
`ALERT_WEBHOOK_TOKEN` env var.

### Generate one token per environment

```bash
openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 40 && echo
```

Run it twice — keep one for DEV (`k8s-ai-test`) and one for PROD (`k8s-devops`).
Store both in your password manager and your gitignored `values-secrets.yaml`.

### Set the tokens via namespace-driven Helm values (recommended)

The chart auto-picks the right token based on `.Release.Namespace`, so a
single `values-secrets.yaml` covers every environment without per-env files:

```yaml
# values-secrets.yaml   (gitignored)
secrets:
  alertWebhookToken_Dev:  "<paste DEV token>"     # used when --namespace k8s-ai-test
  alertWebhookToken_Prod: "<paste PROD token>"    # used when --namespace k8s-devops
```

Then install/upgrade with `--namespace` as the single switch:

```bash
# DEV
helm upgrade --install kubeastra ./helm/kubeastra \
  --namespace k8s-ai-test -f values-secrets.yaml

# PROD — same command, only the namespace changes
helm upgrade --install kubeastra ./helm/kubeastra \
  --namespace k8s-devops -f values-secrets.yaml
```

The lookup order honored by the `kubeastra.alertWebhookToken`
template helper (see `helm/kubeastra/templates/_helpers.tpl`):

1. `secrets.alertWebhookToken` — explicit single-env / legacy override.
2. `secrets.alertWebhookTokensByNamespace[<ns>]` — generic map for >2 envs.
3. `secrets.alertWebhookToken_{Dev,Prod}` — the convenience aliases above.

Leave all three empty for local/dev: the webhook stays open so curl + an
Alertmanager payload work without a header. Don't do that in shared
environments.

### Verify the token landed

```bash
kubectl -n k8s-ai-test get secret kubeastra-secrets \
  -o jsonpath='{.data.ALERT_WEBHOOK_TOKEN}' | base64 -d; echo
# Should print the DEV token byte-for-byte
```

---

## 2. Wire Alertmanager to the webhook

Add a webhook receiver that sends to the assistant's webhook URL with the
bearer token in the `Authorization` header. The exact mechanism depends on
where Alertmanager is reachable from your network and how it's managed.

### 2a. Pick the webhook URL

Two patterns. Pick by where Alertmanager runs:

| Where Alertmanager runs | URL to use |
|---|---|
| **Inside the same cluster** | `http://kubeastra-backend.<ns>.svc.cluster.local:8000/api/v1/alerts/webhook` |
| **Outside the cluster** (or in `monitoring` namespace with NetworkPolicy in the way) | `http://kubeastra-dev.example.com:8000/api/v1/alerts/webhook` (DEV ILB) or `http://kubeastra-prod.example.com:8000/api/v1/alerts/webhook` (PROD) |

The external URLs assume you've enabled the chart's shared-ILB pattern so
the backend Service shares a static internal IP with the frontend. The
chart auto-picks the right IP per namespace via `loadBalancerIPByNamespace`
(set in `values.yaml`) and auto-promotes the backend Service to
`LoadBalancer` with the GCP Internal LB annotation when installing into
a known env. No manual `--set` required:

```bash
helm upgrade --install kubeastra ./helm/kubeastra \
  --namespace k8s-ai-test -f values-secrets.yaml
```

After install, both `frontend` and `backend` Services show the same
`EXTERNAL-IP` (e.g. `10.0.0.100` in DEV), with frontend on `:3000` and
backend on `:8000`. The static IP must have been reserved in GCP with
`--purpose=SHARED_LOADBALANCER_VIP` so both Services can claim it.

### 2b. Create the AM-side Secret

Alertmanager needs to read the same token from its own pod. Create a Secret
in Alertmanager's namespace (typically `monitoring` when using
kube-prometheus-stack):

```bash
TOKEN=$(kubectl -n k8s-ai-test get secret kubeastra-secrets \
          -o jsonpath='{.data.ALERT_WEBHOOK_TOKEN}' | base64 -d)
kubectl -n monitoring create secret generic cortex-webhook-token \
  --from-literal=token="$TOKEN"
```

Sourcing the token from the backend's Secret avoids the "they're not the
same value" failure mode — `diff` of the two should always be empty.

### 2c. Mount the Secret into the Alertmanager pod

For **kube-prometheus-stack** (the common case), add the Secret name to
the `Alertmanager` CR's `spec.secrets` list. The Prometheus Operator
mounts each one at `/etc/alertmanager/secrets/<secret-name>/`, with each
Secret key becoming a file there.

Permanent fix (survives `helm upgrade kube-prometheus-stack`):

```yaml
# kube-prometheus-stack values
alertmanager:
  alertmanagerSpec:
    secrets:
      - cortex-webhook-token
```

Then `helm upgrade kube-prometheus-stack -f <your-values-file> --reuse-values`.

Quick fix (will be wiped on the next chart upgrade):

```bash
kubectl -n monitoring patch alertmanager monitoring-kube-prometheus-alertmanager \
  --type=merge -p '{"spec":{"secrets":["cortex-webhook-token"]}}'
```

Verify the file is now in the pod:

```bash
kubectl -n monitoring exec alertmanager-monitoring-kube-prometheus-alertmanager-0 \
  -c alertmanager -- cat /etc/alertmanager/secrets/cortex-webhook-token/token
# Should print your DEV/PROD token
```

### 2d. Alertmanager config

```yaml
# alertmanager.yaml
route:
  receiver: cortex-webhook
  group_by: [namespace]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 12h    # bump to 24h if your cluster has many always-firing alerts
  routes:
    - matchers: [alertname = "Watchdog"]
      receiver: "null"

receivers:
  - name: "null"

  - name: cortex-webhook
    webhook_configs:
      - url: http://kubeastra-dev.example.com:8000/api/v1/alerts/webhook
        send_resolved: false      # see "Operational notes" — true creates a new investigation per resolve
        http_config:
          authorization:
            type: Bearer
            credentials_file: /etc/alertmanager/secrets/cortex-webhook-token/token
```

The `credentials_file:` path matches what step 2c mounted. Prefer it over
inline `credentials:` so the token never appears in the rendered AM config.

### 2e. (Alternative) Prometheus Operator `AlertmanagerConfig` CR

If your team uses the namespace-scoped `AlertmanagerConfig` CR instead of
the cluster-wide AM config:

```yaml
apiVersion: monitoring.coreos.com/v1alpha1
kind: AlertmanagerConfig
metadata:
  name: cortex-webhook
  namespace: monitoring
spec:
  route:
    receiver: cortex-webhook
    groupBy: [namespace]
    groupWait: 30s
    repeatInterval: 12h
  receivers:
    - name: cortex-webhook
      webhookConfigs:
        - url: http://kubeastra-dev.example.com:8000/api/v1/alerts/webhook
          httpConfig:
            authorization:
              type: Bearer
              credentials:
                name: cortex-webhook-token
                key: token
```

The Operator wires the `credentials` secretKeyRef into the rendered AM
config automatically — no `credentials_file:` mount needed.

---

## 2.5. RCA depth — what each alert actually gets

When an alert lands, the classifier routes it to a specialty playbook by
`alertname` / `reason` / regex match against `classification-rules.yaml`:

| Alertname pattern | Playbook | Investigation depth |
|---|---|---|
| `KubernetesPodCrashLooping`, `*CrashLoop*`, `reason: CrashLoopBackOff` | `crashloopbackoff` | `investigate_pod` runs deterministically first, auto-selecting the failing container (init / main / sidecar). Then branches: `pod_health_baseline`, `pvc_mount_blockers`, `probe_failure`, `oom_memory_termination`. |
| `ContainerOOMKilled`, `KubernetesContainerOomKiller`, `reason: OOMKilled` | `oomkilled` | `investigate_pod` deterministic + `oom_memory_termination` branch. |
| `HighCPUUsage`, `*ContainerCPUThrottling*` | `highcpuusage` | `cpu_resource_base` extension + `prom_query` for throttle/saturation metrics. |
| `DependencyCallTimeout`, `upstream timeout` | `dependency_call_timeout` | dependency_call_timeout playbook with RCA scoring rubric. |
| `KubernetesDeploymentReplicasMismatch`, `KubernetesStatefulSetDown` | `kubernetes_workload_availability` | workload-shaped investigation + pod_health_baseline branch. |
| `LokiRequestErrors`, `LokiIngestionRateLimit` | `loki_logs_health` | Loki-specific log/metrics investigation. |
| `ManualPodInvestigation` (from `/rca`) | `generic-pod` | pod-shaped generic walkthrough. |
| Anything else | `generic` | bare-bones describe + events + logs. |

### Deterministic `investigate_pod` step

The `crashloopbackoff` and `oomkilled` playbooks mark their `investigate_pod`
step `deterministic: true`. The orchestrator runs every deterministic step
**before** the LLM gets to pick anything — so `/rca` and webhook
investigations both call `investigate_pod` first, regardless of what the LLM
would otherwise choose.

This is what gives `/alerts` the same RCA depth as `/chat` on the same pod:
both paths use the same composite tool that classifies the failure mode and
auto-picks the failing container. Without it, the LLM consistently chose
raw `get_pod_logs` (which defaults to the first container in the spec —
typically the main container that never started on a pod with a failing
init container), produced empty log evidence, and reached a "no signal"
conclusion. With it, the actual error (e.g. Jenkins'
`AggregatePluginPrerequisitesNotMetException` in the init container) lands
in the evidence on the first try.

### `prom_query` MCP tool

Specialty playbooks (`highcpuusage`, `oomkilled`, `dependency_call_timeout`,
`crashloopbackoff`) include `prom_query` steps that hit Prometheus directly
for restart rate, CPU throttling, memory pressure, request latency, etc.
Configure the URL in values:

```yaml
backend:
  config:
    prometheusUrl: "http://prometheus-server.monitoring.svc.cluster.local:80"
    prometheusTimeoutSeconds: "10"
```

Fail-soft: when `PROMETHEUS_URL` is empty or Prometheus is unreachable, the
tool returns `{"unavailable": True, "reason": "..."}` and the investigation
completes without metrics evidence (playbook steps that depended on it
still run; they just don't contribute findings).

For Mimir or other Prometheus-compatible backends, the same URL works as
long as the API responds at `/api/v1/query` and `/api/v1/query_range`.
Mimir multi-tenant deployments will also need the `X-Scope-OrgID` header —
not currently wired into `services/prometheus.py`; tracked as a follow-up.

### Tuning LLM concurrency for bursts

When Alertmanager batches many alerts into one POST (the common case —
group_by namespace), the orchestrator fans out into N concurrent
investigations. Each makes 3–5 LLM calls. Without bounds, a 35-alert
burst saturates both the threadpool the synchronous Gemini SDK runs in
and the model's per-minute quota.

The chart exposes a global cap via env var:

```yaml
backend:
  config:
    alertsLlmConcurrency: "8"   # default; raise after quota uplift
```

This caps simultaneous LLM calls across all in-flight investigations.
Excess calls queue gracefully on a per-event-loop semaphore. Set lower
(`4`) if you still see 429s on gemini-3.1-flash-lite; raise (`16`–`32`)
after lifting your Gemini quota in Google Cloud Console.

The LLM calls also run in the FastAPI threadpool (not on the event loop),
so the `/health` liveness probe stays responsive during a burst — no more
pod restarts mid-investigation.

---

## 3. Verify ingestion

After Alertmanager reloads, fire a synthetic alert and watch it land:

```bash
# From inside the cluster (kubectl run --rm -it ...) or via port-forward:
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  --data '{
    "alerts": [{
      "status": "firing",
      "labels": {"alertname": "TestPing", "severity": "info", "namespace": "default"},
      "annotations": {"description": "deploy smoke test"}
    }]
  }' \
  http://kubeastra-backend:8000/api/v1/alerts/webhook
```

Expect a `200` with an `investigation_id`. Confirm in the UI:

```
https://<your-host>/alerts
```

The new investigation should appear at the top of the sidebar within seconds.

---

## 4. Metrics scraping

The backend exposes a Prometheus exposition endpoint at
`/api/v1/metrics`. It is intentionally exempt from session auth so an
in-cluster Prometheus can scrape without credentials. Network reachability is
your security boundary — restrict via NetworkPolicy if scrapers should be
outside the cluster.

### Counters exposed today

| Counter | Increments on |
|---|---|
| `investigations_started_total` | Every orchestrator run begins |
| `investigations_completed_total` | Run reaches `status=completed` |
| `tool_execution_total` | Each playbook tool dispatch |
| `incident_memory_recall_total` | Semantic memory returns ≥1 similar past incident during RCA synthesis |
| `incident_memory_store_failures_total` | Qdrant `store()` raised — investigation completes anyway (fail-soft); a non-zero rate means incident memory is silently dropping records |

### Prometheus scrape config

If you use the Prometheus Operator, drop in a `ServiceMonitor`:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: kubeastra
  namespace: devops
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: kubeastra
      app.kubernetes.io/component: backend
  endpoints:
    - port: http
      path: /api/v1/metrics
      interval: 30s
```

The `matchLabels` should match what the backend `Service` exposes (see
`helm/kubeastra/templates/backend-service.yaml`).

For raw Prometheus config:

```yaml
scrape_configs:
  - job_name: kubeastra
    metrics_path: /api/v1/metrics
    static_configs:
      - targets: ['kubeastra-backend.devops.svc.cluster.local:8000']
```

### Suggested alerts

These translate the counters into the conditions you actually want to wake up
for. Tune to your traffic.

```yaml
groups:
  - name: kubeastra
    rules:
      - alert: AlertManagerIncidentMemoryStoreFailures
        expr: rate(incident_memory_store_failures_total[5m]) > 0
        for: 15m
        labels: { severity: warning }
        annotations:
          summary: "Incident memory is silently dropping records"
          description: "Qdrant store() has been failing for 15m. Investigations still complete, but new RCAs are not landing in semantic memory — recall will degrade until Qdrant recovers."

      - alert: AlertManagerInvestigationsStuckRunning
        expr: increase(investigations_started_total[10m]) - increase(investigations_completed_total[10m]) > 5
        for: 10m
        labels: { severity: warning }
        annotations:
          summary: "Investigations are starting but not completing"
          description: "More than 5 investigations have started without completing in the last 10 minutes — orchestrator may be stuck on kubectl/LLM calls."
```

---

## 5. Operational notes

### Incident memory backing store

Investigations persist to a Qdrant collection named `incident_memory`. It
shares the Qdrant deployment used by the runbook RAG and is bootstrapped on
startup via `services/rag/schema.py`.

If Qdrant is unavailable when an investigation runs, the orchestrator falls
back to an in-process `InMemorySemanticMemoryRepository` for that
investigation — the run completes, but the record is **not** persisted and
`incident_memory_store_failures_total` increments. Once Qdrant is back, new
investigations resume normal persistence; the dropped ones do **not**
backfill automatically.

### Audit trail

Every investigation document carries an `audit_log[]` array recording every
state transition (`alert_received`, `alert_classified`, `playbook_loaded`,
`tool_executed`, `similar_incidents_recalled`, `rca_generated`, etc.). For
manual triggers (`/rca` in chat) the first event is `manual_trigger` and its
payload includes the `user_id` of the triggering user.

The `/alerts` UI surfaces this in the "Audit Timeline" card. Operators can
also pull the raw document via `GET /api/v1/alerts` and `GET /api/v1/alerts`
(filter by investigation id).

### Single-replica constraint

The backend runs as a single replica by design — investigations persist to a
SQLite file on a PVC. Don't scale `backend.replicas` above 1 unless you have
externalized the database. The Helm chart's default keeps this constraint.

### Rotating the webhook token

```bash
NEW=$(openssl rand -hex 32)
helm upgrade kubeastra ./helm/kubeastra \
  --reuse-values --set secrets.alertWebhookToken="$NEW"
# Then update the Alertmanager Secret/AlertmanagerConfig with the new token.
```

The backend reads `ALERT_WEBHOOK_TOKEN` at request time, not at startup, so a
rolling restart isn't required — but Alertmanager will start receiving 401s
the moment the new token rolls out until its own config catches up. Sequence:
update the assistant first, then Alertmanager.
