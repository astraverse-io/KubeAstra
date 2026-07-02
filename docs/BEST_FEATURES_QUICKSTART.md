# Best-Features Quickstart — Qdrant + RAG end-to-end

A detailed operator playbook for going from **nothing installed** to **every Phase 1 feature live**: Qdrant deployed, team docs ingested, the deployment-repo KB indexing, the session-capture flywheel running, and production hardening applied. Total time: ~45 minutes including the first reindex.

After this, end users get cached answers for repeat issues, grounded LLM responses with clickable citations, and the KB grows from real conversations.

> **Companion docs:**
> - [K8S_DEPLOYMENT_GUIDE.md](K8S_DEPLOYMENT_GUIDE.md) — general chart deployment notes
> - `QDRANT_DEPLOYMENT_GUIDE.md` *(internal — every Helm knob, rollback matrix, security wiring details; ask a maintainer if you need it)*
> - `DEPLOYMENT_REPO_KB_USER_GUIDE.md` *(internal — Phase 1.5 in-depth walkthrough)*

---

## Table of contents

- [What "best features" gets you](#what-best-features-gets-you)
- [Architecture in one picture](#architecture-in-one-picture)
- [Prerequisites — what you need before starting](#prerequisites--what-you-need-before-starting)
- [Step 0 · Bootstrap the chart (if not already installed)](#step-0--bootstrap-the-chart-if-not-already-installed)
- [Step 1 · Deploy Qdrant + enable the router](#step-1--deploy-qdrant--enable-the-router)
- [Step 2 · Ingest your team docs](#step-2--ingest-your-team-docs)
- [Step 3 · Enable the deployment-repo KB (Phase 1.5)](#step-3--enable-the-deployment-repo-kb-phase-15)
- [Step 4 · Turn on session capture — the flywheel](#step-4--turn-on-session-capture--the-flywheel)
- [Step 4.5 · Phase 2 + 3 — memory, prompt cache, proactive triage](#step-45--phase-2--3--memory-prompt-cache-proactive-triage)
- [Step 5 · Production hardening](#step-5--production-hardening)
- [Step 6 · Tune router thresholds from real data](#step-6--tune-router-thresholds-from-real-data-after-1-week)
- [Verification checklist — you're done when…](#verification-checklist--youre-done-when)
- [The final values.yaml — single artifact](#the-final-valuesyaml--single-artifact)
- [End-to-end reproducible script](#end-to-end-reproducible-script)
- [Troubleshooting](#troubleshooting)
- [Cost / footprint reality check](#cost--footprint-reality-check)

---

## What "best features" gets you

When everything below is enabled, here's what users see vs the bare assistant:

| Capability | Without RAG | With full RAG |
|---|---|---|
| Repeat question | LLM regenerates from scratch every time | **Cached** answer from a verified runbook — zero LLM call |
| Ansible / playbook errors | Generic Ansible knowledge | Grounded with actual playbook content + clickable GitHub citations |
| Team-specific conventions | Often wrong | Pulled from your team's runbooks |
| KB freshness | Static | Auto-grows from every resolved chat the team thumbs-up's |
| Audit / observability | Just LLM tool calls | Every router decision logged with score, collection, citations |
| Real-time alert ingestion | Manual digging in Grafana / `kubectl describe` | **`/alerts`** page receives Prometheus Alertmanager webhooks; specialty playbook auto-routes by alertname; deterministic `investigate_pod` produces verified RCA |
| Ad-hoc pod investigation | `kubectl describe`, `kubectl logs`, eyeball the output | **`/rca pod-name`** in chat — auto-discovers namespace, detects pod state, runs the same depth as a real alert. |

---

## Try the `/rca` slash command in 30 seconds

You don't need the full RAG setup for this — `/rca` works as soon as the
chart is installed and the backend can `kubectl exec` against your cluster.

1. Open the chat UI at `https://<your-host>:3000/chat`.
2. Type `/rca <pod-name>` (e.g. `/rca jenkins-legacy-0`).
3. The slash command intercepts and posts to `/api/v1/alerts/manual`.
   The backend auto-discovers the namespace via `find_workload`, reads the
   pod's effective status, and aliases the synthetic alertname to a
   specialty playbook when the pod is in `CrashLoopBackOff` or `OOMKilled`.
4. Within ~15-30 seconds an investigation appears at the top of the
   `/alerts` page. Click it to see:
   - **Playbook:** `crashloopbackoff` (or whichever specialty matched)
   - **Executed tools:** `investigate_pod` runs **first** (deterministic),
     followed by LLM-chosen follow-ups
   - **Findings:** RCA with confidence score, ruled-out alternatives, and
     recommended actions
   - **Audit timeline:** every action validated and dispatched

If you fire `/rca` on a healthy pod, you get the simpler `generic-pod`
walkthrough instead.

For real Alertmanager-fed alerts, see
[alert_manager_deploy.md](alert_manager_deploy.md) — the same
specialty playbooks fire automatically when AM posts to the webhook.

---

## Architecture in one picture

```
                                    ┌──────────────────┐
                                    │  Chat user       │
                                    └────────┬─────────┘
                                             │ HTTPS
                                             ▼
                              ┌─────────────────────────────────┐
                              │  Backend (FastAPI)              │
                              │  /api/chat[/stream]             │
                              └──┬─────────────────┬────────────┘
                                 │                 │
              vector search      │                 │  LLM call
              (parallel,         ▼                 ▼
               2-3 collections)  ┌─────────┐    ┌────────┐
                       ┌────────►│  Qdrant │    │ Gemini │
                       │         │  StSet  │    └────────┘
                       │         └────┬────┘
                       │              │
        ┌──────────────┴──┐           │ persists to
        │  Router         │           ▼
        │  cached/        │       ┌─────┐
        │  grounded/      │       │ PVC │
        │  cold           │       └─────┘
        └──┬──────────────┘
           │
           │ writes captured chats
           │ promotes to runbook on 👍
           └──────────────────────────┐
                                      │
            ┌─────────────────────────┴───────────┐
            │  rag-ingestion CronJob (nightly)    │
            │  - clones github source(s)          │
            │  - chunks + embeds                  │
            │  - upserts to Qdrant                │
            └─────────────────────────────────────┘
```

**Three write paths** populate Qdrant:
1. **Nightly CronJob** ingests team docs (markdown ConfigMap / git repo / future source kinds)
2. **Same CronJob** also indexes the deployment repo (Phase 1.5) when enabled
3. **Live chat capture** writes resolved conversations to `session_memory` (Phase 1.3); operator-side **thumbs-up** promotes them to `runbook` with `verified: true`

The backend reads from Qdrant on every chat turn (never writes); it never touches GitHub or the docs source directly.

---

## Prerequisites — what you need before starting

Run these and confirm before continuing:

```bash
# 1. CLI tools (versions tested)
kubectl version --client --short       # expect: Client Version: v1.27+
helm version --short                    # expect: v3.13.0+

# 2. Cluster access
kubectl cluster-info                    # should print the control plane URL
kubectl auth can-i create deployments -n k8s-devops    # → "yes"
kubectl auth can-i create cronjobs -n k8s-devops       # → "yes"
kubectl auth can-i create statefulsets -n k8s-devops   # → "yes"

# 3. Default StorageClass exists (the chart needs one for Qdrant PVC)
kubectl get storageclass
# Look for "(default)" on one of the classes. If none, see "Troubleshooting → No
# default StorageClass" below.

# 4. CNI that enforces NetworkPolicy (required for production hardening at step 5)
kubectl get pods -n kube-system | grep -E 'cilium|calico|gke-cilium'
# GKE/EKS native CNIs enforce NetworkPolicy. Older kops/kubeadm may not.

# 5. Image registry reachable from the cluster
# Default chart uses: ghcr.io/kubeastra/k8s-devops-{backend,frontend}
# If your nodes can't pull from that, override backend.image.repository and
# frontend.image.repository in values to a registry they CAN reach.
```

You'll also need:

- [ ] **Gemini API key** — get one at https://aistudio.google.com/ (free tier is enough for evaluation)
- [ ] **Cluster kubeconfig** the agent will use to talk to your cluster — typically `~/.kube/config` or a service account's config you've extracted
- [ ] **GitHub fine-grained PAT** on `kubeastra/deployment-provisioning` with read-only `Contents` permission (only needed for Step 3 — Phase 1.5)
- [ ] **A team-docs source** — either a folder of markdown runbooks or a private/public git repo

---

## Step 0 · Bootstrap the chart (if not already installed)

If the chart is already deployed and running, skip to Step 1. Otherwise:

### 0a. Create the namespace and base secrets

```bash
# Namespace
kubectl create namespace k8s-devops

# Secrets: Gemini API key, kubeconfig the agent uses for kubectl, MCP bearer token.
# These are referenced from the chart Secret template
# (helm/kubeastra/templates/secret.yaml).
GEMINI_KEY="<paste-your-key>"
KUBECONFIG_B64=$(cat ~/.kube/config | base64 | tr -d '\n')
MCP_TOKEN=$(openssl rand -hex 32)

# Method A: pass via --set on `helm install` below.
# Method B: write a values-secrets.yaml (gitignored) and pass with -f:
cat > values-secrets.yaml <<EOF
secrets:
  geminiApiKey: "${GEMINI_KEY}"
  kubeconfig:   "${KUBECONFIG_B64}"
  mcpAuthToken: "${MCP_TOKEN}"
EOF
chmod 600 values-secrets.yaml
```

### 0b. Minimal `my-values.yaml` for first install

```yaml
# my-values.yaml — minimum to get the chart running. We layer feature
# flags on top in later steps.
namespace: k8s-devops

backend:
  image:
    repository: ghcr.io/kubeastra/kubeastra-backend
    tag: "main-b174261"           # or your CI-built tag
  replicaCount: 1

frontend:
  image:
    repository: ghcr.io/kubeastra/kubeastra-frontend
    tag: "main-b174261"
  replicaCount: 1
  service:
    type: LoadBalancer            # exposes the chat UI; use ClusterIP if you'll port-forward
```

### 0c. Install

```bash
helm install k8s-devops ./helm/kubeastra \
  -n k8s-devops \
  -f my-values.yaml \
  -f values-secrets.yaml          # only if you used Method B

# Watch pods come up (~30-60s)
kubectl get pods -n k8s-devops -w
```

**Expected** after ~1 minute:

```
NAME                                       READY   STATUS    RESTARTS   AGE
kubeastra-backend-xxx           1/1     Running   0          45s
kubeastra-frontend-xxx          1/1     Running   0          45s
```

Note: no `qdrant-*` pod yet — that comes in Step 1.

### 0d. Get the frontend URL

```bash
kubectl get svc -n k8s-devops kubeastra-frontend
# Look at EXTERNAL-IP (LoadBalancer). Browse to http://<ip>:3000
# If still <pending>, your cloud's LB hasn't provisioned yet — wait 1-2 min
# or use port-forward: kubectl port-forward svc/kubeastra-frontend 3000:3000 -n k8s-devops
```

Open the URL → confirm chat works (ask anything; it'll respond using just Gemini, no RAG yet).

> **Sanity check:** if you see "no LLM provider configured" or auth errors, your `geminiApiKey` secret didn't take. Re-run `kubectl describe secret kubeastra-secrets -n k8s-devops` and confirm `GEMINI_API_KEY` is listed.

---

## Step 1 · Deploy Qdrant + enable the router

The chart ships Qdrant as a single-replica StatefulSet with a PVC. Default sizing handles tens of thousands of vectors comfortably.

### 1a. Add to `my-values.yaml`

```yaml
qdrant:
  enabled: true                       # deploys the StatefulSet + Service + PVC
  image:
    repository: qdrant/qdrant
    tag: "v1.11.0"
  storage:
    size: 5Gi                         # plenty for a team-sized KB
    storageClassName: ""              # "" = cluster default; pin a specific SC for prod
  resources:
    requests:
      cpu: 200m
      memory: 512Mi
    limits:
      cpu: 1
      memory: 2Gi

backend:
  config:
    # The router is on by default but a no-op until Qdrant has content.
    # Listed here so its value is explicit alongside the other knobs.
    ragRouterEnabled: "true"
    ragRouterTopK: "5"
    ragRouterCachedThreshold: "0.92"
    ragRouterGroundedThreshold: "0.70"
    # Note: default already includes deployment_repo. Override here if needed.
    ragRouterCollections: "runbook,devops_doc,deployment_repo"
```

### 1b. Apply

```bash
helm upgrade k8s-devops ./helm/kubeastra \
  -n k8s-devops -f my-values.yaml -f values-secrets.yaml

# Wait for Qdrant
kubectl rollout status statefulset/kubeastra-qdrant -n k8s-devops --timeout=120s
```

### 1c. Verify Qdrant is healthy

```bash
# Pod up
kubectl get pods -n k8s-devops -l app.kubernetes.io/component=qdrant
# NAME                              READY   STATUS    RESTARTS   AGE
# kubeastra-qdrant-0     1/1     Running   0          1m

# PVC bound
kubectl get pvc -n k8s-devops -l app.kubernetes.io/component=qdrant
# NAME                                  STATUS   VOLUME    CAPACITY   ACCESS MODES
# data-kubeastra-qdrant-0    Bound    pvc-xxx   5Gi        RWO

# Service reachable
kubectl exec -n k8s-devops kubeastra-qdrant-0 -- \
  curl -s http://localhost:6333/readyz
# Expected: "all shards are ready"

# Collections endpoint returns empty list (we'll fill it next)
kubectl exec -n k8s-devops kubeastra-qdrant-0 -- \
  curl -s http://localhost:6333/collections | head -c 200
# Expected: {"result":{"collections":[]},"status":"ok",...}
```

### 1d. Confirm the backend can reach Qdrant

```bash
kubectl exec -n k8s-devops deploy/kubeastra-backend -- \
  env | grep QDRANT
# Expected:
# QDRANT_URL=http://kubeastra-qdrant:6333
# QDRANT_COLLECTION=k8s_errors
# QDRANT_TIMEOUT_SECONDS=10

# Send a chat that should produce a cold router decision (nothing indexed yet)
kubectl port-forward -n k8s-devops svc/kubeastra-backend 8000:8000 &
sleep 2
curl -sS -X POST http://localhost:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "what time is it", "history": []}' \
  | jq '.result.rag_decision'
# Expected: {"mode": "cold", "top_score": 0.0, ...}
kill %1
```

**What you have now:** healthy Qdrant pod, empty collections, backend successfully querying it (just getting `cold` results because there's no content).

---

## Step 2 · Ingest your team docs

Pick one of two options. Option A is fastest; Option B is better long-term.

### Option A — ConfigMap of markdown (5 min)

```bash
# Drop a few runbooks in a local folder
mkdir -p ./team-runbooks
cat > ./team-runbooks/crashloop.md <<'EOF'
# CrashLoopBackOff playbook

Pods that keep restarting usually fail their liveness probe or panic on startup.

## Diagnose
Run `kubectl describe pod` and check Last State / Reason.

## Common fixes
- Bump memory limits if Reason is OOMKilled
- Check ConfigMap mounts and env vars
EOF

cat > ./team-runbooks/imagepull.md <<'EOF'
# ImagePullBackOff
Symptoms: pod stuck in ImagePullBackOff or ErrImagePull.

## Diagnose
kubectl describe pod | grep -A5 Events

## Fix
- Check imagePullSecrets reference the correct Secret
- Verify the image tag actually exists in the registry
- Pull manually from a node: crictl pull <image>
EOF

# Make it visible to the CronJob (chart mounts it at /knowledge)
kubectl create configmap team-runbooks --from-file=./team-runbooks/ -n k8s-devops
```

### Add to `my-values.yaml`

```yaml
rag:
  enabled: true                                # master switch for the CronJob
  ingestion:
    schedule: "0 2 * * *"                      # nightly at 02:00 cluster-local
    knowledgeVolume:
      enabled: true
      mountPath: /knowledge
      configMapName: team-runbooks             # the ConfigMap you created
    # The default rag.ingestion.config.sources already includes:
    #   - kind: local_path
    #     path: /knowledge
    # which reads from the mounted ConfigMap.
    resources:
      requests:
        cpu: 200m
        memory: 1Gi
      limits:
        cpu: 1
        memory: 2Gi
```

### Option B — Git repo of runbooks (better long-term)

Skip the ConfigMap. Instead:

```bash
# 1. Create a Secret holding your GitHub PAT (only needed for private repos)
kubectl create secret generic runbooks-token \
  --from-literal=token=ghp_xxx \
  -n k8s-devops
```

```yaml
# my-values.yaml — replace the local_path source with a git_repo source
rag:
  enabled: true
  ingestion:
    schedule: "0 2 * * *"
    config:                                   # ← overrides the chart default
      sources:
        - kind: git_repo
          url: https://github.com/your-org/team-runbooks
          branch: main
          subdir: docs                        # optional, only walk this subfolder
          token_env: GITHUB_TOKEN             # for private repos
      chunking:
        max_tokens: 400
        overlap_tokens: 60
    extraEnvFromSecrets:
      GITHUB_TOKEN:
        secretName: runbooks-token
        key: token
```

### 2c. Apply and trigger the first ingest

```bash
helm upgrade k8s-devops ./helm/kubeastra \
  -n k8s-devops -f my-values.yaml -f values-secrets.yaml

# Confirm the CronJob was created
kubectl get cronjob -n k8s-devops
# NAME                                       SCHEDULE     SUSPEND   ACTIVE
# kubeastra-rag-ingestion         0 2 * * *    False     0

# Confirm the rag-config ConfigMap was rendered correctly
kubectl get configmap -n k8s-devops kubeastra-rag-config -o yaml | tail -20
# Should show your source list plus chunking section

# Don't wait for 02:00 — fire the bootstrap run now
kubectl create job --from=cronjob/kubeastra-rag-ingestion \
  rag-bootstrap-docs -n k8s-devops

# Watch logs
kubectl logs -f -n k8s-devops job/rag-bootstrap-docs
```

**Expected log progression:**

```
INFO services.embeddings Loading embedding model: sentence-transformers/all-MiniLM-L6-v2
INFO sentence_transformers ... Use pytorch device_name: cpu
Loading weights: 100%|██████████| 103/103 [00:00<00:00, ...]
INFO rag.reindex Ingesting source kind=local_path into collection=devops_doc
INFO services.vector_db Created Qdrant collection: devops_doc
INGEST_SUMMARY {"discovered": 2, "chunks_seen": 5, "new": 5, "skipped": 0, "failed": 0, "duration_seconds": 11.4}
```

The first run downloads the ~85 MB embedding model. Subsequent runs use the cached model and are much faster.

### 2d. Try a chat — confirm router fires

```bash
kubectl port-forward -n k8s-devops svc/kubeastra-backend 8000:8000 &
sleep 2
curl -sS -X POST http://localhost:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "my pod keeps crashlooping in prod", "history": []}' \
  | jq '.result.rag_decision'
kill %1
```

**Expected:**

```json
{
  "mode": "grounded",
  "top_score": 0.84,
  "top_collection": "devops_doc",
  "citations": [
    {"title": "crashloop.md", "url": "file:///knowledge/crashloop.md",
     "section": "CrashLoopBackOff playbook > Common fixes", "similarity": 0.84}
  ],
  "reason": "top hit in devops_doc (similarity=0.840)",
  "ansible_detected": false
}
```

You can also watch the audit log live:

```bash
kubectl exec -n k8s-devops deploy/kubeastra-backend -- \
  tail -f /app/audit.log | grep RAG_ROUTE
```

---

## ⚡ Single-command path (since chart v1.0.0+)

If you don't want to walk through every step below, the chart now ships with `values-production.yaml` that flips every Phase 1 feature on in one go. After Step 0 (chart bootstrapped + namespace + base secrets), this single command replaces Steps 1–5 below:

```bash
helm upgrade k8s-devops ./helm/kubeastra \
  -n k8s-devops --reuse-values \
  -f helm/kubeastra/values-production.yaml \
  --set deploymentRepo.token=ghp_xxxxxxxxxxxxxxxxxxxx
```

What it does automatically:

- Deploys Qdrant (`qdrant.enabled=true`) with NetworkPolicy
- Templates the `deployment-repo-token` K8s Secret from the inline `--set` (or skip the `--set` and create the Secret externally with sealed-secrets / external-secrets-operator)
- Enables the RAG ingestion CronJob (`rag.enabled=true`)
- Enables the deployment-repo KB source (`deploymentRepo.enabled=true`)
- Enables session capture + feedback flywheel (`sessionCaptureEnabled=true`)
- Fires a one-shot **bootstrap Job** as a post-install/post-upgrade hook so the first reindex starts immediately. Watch with:
  ```bash
  kubectl logs -f -n k8s-devops job/kubeastra-rag-bootstrap
  ```

Use the manual steps below if you'd rather enable features piecemeal, or to learn the underlying knobs.

---

## Step 3 · Enable the deployment-repo KB (Phase 1.5)

This is the Phase 1.5 feature — Ansible-aware indexing of the deployment repo with auto-detected routing for Ansible-flavored errors. Detailed steps below; the full historical design lives in the internal `DEPLOYMENT_REPO_KB_USER_GUIDE.md` if you need more depth.

### 3a. Create the GitHub PAT secret

In GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new:

| Field | Value |
|---|---|
| Token name | `kubeastra-deployment-repo` |
| Resource owner | `kubeastra` |
| Repository access | Only select repositories → `deployment-provisioning` |
| Permissions → Repository | **Contents: Read-only** (nothing else) |
| Expiration | per your org policy (90 days recommended) |

Then:

```bash
kubectl create secret generic deployment-repo-token \
  --from-literal=token=ghp_xxxxxxxxxxxxxxxxxxxxxxxx \
  -n k8s-devops

# Confirm
kubectl describe secret deployment-repo-token -n k8s-devops | grep token
# token:  44 bytes      ← length will vary by token type
```

### 3b. Add to `my-values.yaml`

```yaml
deploymentRepo:
  enabled: true
  url: "https://github.com/kubeastra/deployment-provisioning.git"
  branch: "main"
  subdir: "ansible"                          # walk only the ansible/ subdir
  tokenSecretName: "deployment-repo-token"
  tokenSecretKey: "token"
```

### 3c. Apply

```bash
helm upgrade k8s-devops ./helm/kubeastra \
  -n k8s-devops -f my-values.yaml -f values-secrets.yaml

# Verify the rag-config ConfigMap now includes the deployment-repo source
kubectl get cm kubeastra-rag-config -n k8s-devops -o jsonpath='{.data.config\.yaml}'
# Expected: sources: list now contains a git_repo entry with
#   url, branch, subdir: ansible, emit_role_aggregates: true,
#   collection: deployment_repo

# Verify the CronJob env now includes GITHUB_TOKEN
kubectl get cronjob kubeastra-rag-ingestion -n k8s-devops \
  -o yaml | grep -A4 GITHUB_TOKEN
# Should show secretKeyRef -> deployment-repo-token / token
```

### 3d. Bootstrap the deployment-repo index

```bash
kubectl create job --from=cronjob/kubeastra-rag-ingestion \
  rag-bootstrap-deployrepo -n k8s-devops

kubectl logs -f -n k8s-devops job/rag-bootstrap-deployrepo
```

**Expected progression** (this run is ~10–15 minutes):

```
INFO rag.reindex Ingesting source kind=local_path into collection=devops_doc
INGEST_SUMMARY {"discovered": 2, "chunks_seen": 5, "new": 0, "skipped": 5, ...}   ← unchanged docs

INFO rag.reindex Ingesting source kind=git_repo into collection=deployment_repo
INFO services.vector_db Created Qdrant collection: deployment_repo
... (git clone, embedding ~4200 chunks)
INGEST_SUMMARY {"discovered": 1365, "chunks_seen": 4240, "new": 4240, "skipped": 0, "failed": 0, "duration_seconds": 880.4}
```

### 3e. Verify

```bash
# Point count
kubectl exec -n k8s-devops kubeastra-qdrant-0 -- \
  curl -s http://localhost:6333/collections/deployment_repo | jq '.result.points_count'
# Expected: ~4200

# Try an Ansible error paste
kubectl port-forward -n k8s-devops svc/kubeastra-backend 8000:8000 &
sleep 2
curl -sS -X POST http://localhost:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"TASK [kubernetes/kube_check_health : Check kubernetes Nodes] *** fatal: [worker-3]: FAILED! kubernetes.core.k8s_info: Failed to import the required Python library (kubernetes)","history":[]}' \
  | jq '.result.rag_decision | {mode, top_score, top_collection, ansible_detected, citations: [.citations[] | .url]}'
kill %1
```

**Expected:**

```json
{
  "mode": "grounded",
  "top_score": 0.74,
  "top_collection": "deployment_repo",
  "ansible_detected": true,
  "citations": [
    "https://github.com/kubeastra/deployment-provisioning/blob/main/ansible/roles/kubernetes/kube_check_health/...",
    "https://github.com/kubeastra/deployment-provisioning/blob/main/ansible/roles/kubernetes/..."
  ]
}
```

`ansible_detected: true` confirms the router's Ansible regex fired and force-included `deployment_repo`. Citations link to clickable GitHub blob URLs.

---

## Step 4 · Turn on session capture — the flywheel

Without this, every chat is independent. With it, the team's good answers automatically populate `session_memory`, and thumbs-up promotes them to `runbook` for instant cached responses on repeat questions.

### 4a. Add to `my-values.yaml`

```yaml
backend:
  config:
    sessionCaptureEnabled: "true"
    sessionCaptureTtlDays: "90"                       # unverified entries expire
    sessionCaptureTranscriptChars: "4000"             # soft cap on classifier prompt size
    sessionCaptureRedactSecrets: "true"               # MANDATORY in production
    # Don't add session_memory to ragRouterCollections yet — wait a week and
    # eyeball captures first (see step 4d). When you trust them:
    #   ragRouterCollections: "runbook,devops_doc,deployment_repo,session_memory"
```

### 4b. Apply

```bash
helm upgrade k8s-devops ./helm/kubeastra \
  -n k8s-devops -f my-values.yaml -f values-secrets.yaml

# Backend picks up env changes on pod restart
kubectl rollout restart deploy/kubeastra-backend -n k8s-devops
kubectl rollout status deploy/kubeastra-backend -n k8s-devops --timeout=60s
```

### 4c. How the flywheel works at runtime

1. User asks a question, agent answers (grounded or cold)
2. A cheap classifier call (Gemini Flash) decides: *did this chat solve a real problem?*
3. If yes → written to `session_memory` with `verified: false`, 90-day TTL stamp
4. UI shows 👍 / 👎 next to the assistant message
5. 👍 → entry copied to `runbook` with `verified: true`, original deleted from `session_memory`
6. **Next** person who asks something similar → router's **cached** path fires (similarity ≥ 0.92 + verified) → instant answer, zero LLM call

**Privacy:** every captured payload runs through [`services/rag/redaction.py`](../mcp/services/rag/redaction.py) first — GitHub/AWS/Google/Slack API key patterns, JWT, kubeconfig tokens, PEM blocks, long base64 blobs. Leave `sessionCaptureRedactSecrets=true`.

### 4d. Watch capture happen

```bash
# Generate some chat traffic via the UI

# Track session_memory growth
watch -n 10 'kubectl exec -n k8s-devops kubeastra-qdrant-0 -- \
  curl -s http://localhost:6333/collections/session_memory | jq .result.points_count'

# After a few thumbs-ups, watch runbook grow + session_memory shrink
watch -n 10 'kubectl exec -n k8s-devops kubeastra-qdrant-0 -- \
  bash -c "curl -s http://localhost:6333/collections/runbook | jq .result.points_count; \
           curl -s http://localhost:6333/collections/session_memory | jq .result.points_count"'
```

---

## Step 4.5 · Phase 2 + 3 — memory, prompt cache, proactive triage

Steps 1-4 cover the Phase 1.x flywheel (Qdrant + RAG + ingestion + deployment-repo KB + session capture). Phase 2 and 3 layer on top: **per-user conversation memory**, **semantic prompt cache** (an L2 cache against captured sessions), and **proactive cluster triage** on the first chat of every session.

All three are on by default in `values-production.yaml`. This section is a quick tour so you know what's running, what to look for in logs, and what to disable if it gets in the way.

### 4.5a. Conversation memory (Phase 2.2)

`ui/backend/memory.py` keeps a lightweight "you've been working on X" record per session. On every successful tool dispatch inside the ReAct loop, it captures the namespaces, resources, tools, and clusters the user is touching. On subsequent turns it renders that as a short preamble prepended to the LLM prompt.

**What you get:** the agent stops re-asking "which namespace?" when the user's been working in one for the last 10 turns.

**Where it lives:** a single JSON row per session in SQLite (table `user_memory`). No new infrastructure. Top N=10 per category, sorted by recency.

**Knobs:** none worth touching for v1. The capture point is inside the ReAct loop, so it follows whatever the agent does — if you start using a path that bypasses ReAct (e.g. the legacy `/api/chat` sync endpoint with `useReactChat: false`), memory won't capture from that path.

**To watch it work:**
```bash
# After a few chats in one session
sqlite3 /app/data/chat_history.db \
  "select session_id, json_extract(memory, '$.namespaces') from user_memory limit 5;"
```

### 4.5b. Semantic prompt cache (Phase 2.3)

`mcp/services/rag/prompt_cache.py` adds an L2 cache *between* the user's question and the router. Before classifying or calling the LLM at all, it checks whether a nearly-identical question was answered recently in this team's session log.

| | Phase 1.4 "cached" mode | Phase 2.3 prompt cache |
|---|---|---|
| Source | `runbook` collection | `session_memory` collection |
| Trust requirement | `verified=True` (human 👍'd) | None |
| Similarity threshold | 0.92 | 0.95 (stricter) |
| Lookback | Forever (TTL = no expiry on runbook) | Bounded — recent N hours only |
| What it catches | Issues the team has explicitly endorsed | Paraphrases of questions someone *just* asked today |

**What you get:** if Alice asked "ImagePullBackOff on api-server pod" at 10am and Bob types "api-server keeps failing image pull" at 2pm, Bob gets Alice's answer in ~50ms with zero LLM call.

**Where to look:** SSE events emit `event: kb_route` with `mode=cached` and a `cache_source` field distinguishing 1.4 vs 2.3. Both bypass the ReAct loop entirely.

**To verify it's hitting:** after the team uses it for a few days, grep backend logs:
```bash
kubectl logs -n k8s-devops -l app.kubernetes.io/component=backend \
  --since=24h | grep -E "prompt_cache_hit|kb_route.*cached"
```

If `session_memory` is empty or thin (you can see point counts via the Qdrant API), the cache has nothing to match against — it'll warm up over the first week of real usage.

### 4.5c. Proactive cluster triage (Phase 3.0)

`ui/backend/triage.py` runs a fast read-only scan on the user's currently-selected cluster context **on the first message of every chat session** and surfaces obvious problems before the user finishes typing. The chat router emits a `triage_greet` SSE event right before the answer stream starts, so users see "I noticed `api-7c4d6` is in CrashLoopBackOff and 3 pods are stuck Pending in `staging`. Want me to investigate?" without having to ask.

**What it does:**
- Reuses `get_pods` and `get_events` from `k8s/wrappers.py` — no new MCP tools, no LLM call.
- Reports CrashLooping pods, Pending pods (with age filter), recent Warning events (configurable lookback).
- Stateless — every session re-scans (no dedup; Phase 3.1 would change that).
- Read-only — no destructive ops.

**Flags in `my-values.yaml`:**
```yaml
enableProactiveTriage: true                  # off by default in base values.yaml,
                                              # on by default in values-production.yaml
proactiveTriageNamespaces: "*"                # csv list or "*"
proactiveTriageEventLookbackMin: 10           # int minutes; default 10
```

**Failure modes:** wrapped in try/except — any failure (kubeconfig missing, kubectl timeout, network blip) is logged at debug level and the chat proceeds normally. Triage is **never** allowed to block the user's question.

**To disable:** set `enableProactiveTriage: false` and helm upgrade. Recommended only if your team finds the greeting noisy.

### 4.5d. Startup ops you should know about

Two things happen automatically at backend pod startup that affect Phase 1-3 features. You don't have to flip them on — they're always-on — but you should know what to look for if something breaks.

**1. RAG collection bootstrap.** `main.py:_bootstrap_rag_collections` runs in the FastAPI lifespan hook. It connects to Qdrant and calls `ensure_collection_for` on `runbook`, `devops_doc`, `deployment_repo`, `session_memory`. Without this, the first chat used to spam 404s for any collection that hadn't been touched yet (e.g. `runbook` is empty until the first thumbs-up). Now they always exist.

```
kubectl logs -n k8s-devops -l app.kubernetes.io/component=backend | grep "RAG bootstrap"
# Expect: "RAG bootstrap: ensured runbook, devops_doc, deployment_repo, session_memory"
```

**2. Embedding model pre-warm.** Same lifespan hook also runs one throwaway `embeddings.embed("warmup")` so the sentence-transformer model is loaded into RAM before any user shows up. Without this, the first chat ate a 5-10s pause while the model went from `/tmp/hf-cache` → RAM. Watch for:

```
RAG bootstrap: embedding model warmed
```

If either log line is missing but the cluster is otherwise healthy, you're likely hitting the uvicorn-stdout-vs-Python-logger race that swallows lifespan log lines on some configs. The *effects* (collections exist, first chat is snappy) are what matter — check those directly via the Qdrant API and a first-chat timing test.

**3. Qdrant client/server version pin.** `qdrant-client` in `requirements.txt` is pinned to match the deployed Qdrant server's minor (`>=1.11.0,<1.12.0` against Qdrant `v1.11.x`). When you bump Qdrant's StatefulSet image tag, bump this pin in the same commit — the client emits a hard warning and risks silent API drift when minors diverge by more than 1.

---

## Step 5 · Production hardening

Three additions before you call this prod-ready.

### 5a. NetworkPolicy — restrict Qdrant ingress

Without NetworkPolicy, **any pod in any namespace** can query Qdrant. Lock it down.

```yaml
qdrant:
  networkPolicy:
    enabled: true                # restricts ingress to backend + ingestion CronJob
```

```bash
helm upgrade k8s-devops ./helm/kubeastra \
  -n k8s-devops -f my-values.yaml -f values-secrets.yaml

# Verify the policy exists
kubectl get networkpolicy -n k8s-devops
# NAME                              POD-SELECTOR
# kubeastra-qdrant       app.kubernetes.io/component=qdrant
```

**Verify enforcement** — try to reach Qdrant from an unrelated pod:

```bash
kubectl run -it --rm test --image=curlimages/curl --restart=Never \
  -n k8s-devops -- curl -m 5 http://kubeastra-qdrant:6333/readyz
# Should hang and exit with timeout — that's correct (denied)

# Confirm the backend can still reach it
kubectl exec -n k8s-devops deploy/kubeastra-backend -- \
  curl -s -m 5 http://kubeastra-qdrant:6333/readyz
# Should print: "all shards are ready"
```

> If the test pod *can* reach Qdrant, your CNI isn't enforcing NetworkPolicy. Check that you're on cilium / calico / GKE Dataplane V2.

### 5b. Qdrant API key auth (optional — see caveat)

```bash
kubectl create secret generic qdrant-api-key \
  --from-literal=apiKey=$(openssl rand -hex 32) -n k8s-devops
```

```yaml
qdrant:
  auth:
    apiKeySecretRef: qdrant-api-key
    apiKeySecretKey: apiKey
```

> ⚠️ **Known wiring gap** (tracked in the internal `QDRANT_DEPLOYMENT_GUIDE.md` §C1 *API key auth on Qdrant*): the chart wires the key to the Qdrant pod itself but **not** to the backend/MCP/ingestion pods that connect to it. Until that gap is closed, NetworkPolicy (5a) is your effective auth mechanism. Leave `qdrant.auth.apiKeySecretRef` empty unless you also manually inject `QDRANT_API_KEY` into the backend.

### 5c. Pin a storage class with backups

```yaml
qdrant:
  storage:
    size: 5Gi
    storageClassName: "standard-rwo"        # or your provider's equivalent w/ snapshots
                                            # GKE: standard-rwo / premium-rwo
                                            # EKS: gp3 / io2
                                            # Azure: managed-csi-premium
```

The KB is recoverable even from total loss — re-running the ingestion CronJob rebuilds from the source-of-truth ConfigMap/git repo and the deployment repo. Snapshots are nice-to-have, not load-bearing.

### 5d. Optional: scheduled Qdrant snapshots

If you want belt-and-braces backups:

```bash
# One-off snapshot of a collection (writes to the PVC)
kubectl exec -n k8s-devops kubeastra-qdrant-0 -- \
  curl -X POST http://localhost:6333/collections/devops_doc/snapshots
# Snapshots land in /qdrant/storage/snapshots/<collection>/
# Copy them out via kubectl cp, or use Velero for cluster-wide PVC snapshots.
```

---

## Step 6 · Tune router thresholds from real data (after ~1 week)

Defaults are conservative guesses. Tune from actual traffic.

```bash
# Collect a week of decisions
kubectl exec -n k8s-devops deploy/kubeastra-backend -- \
  tail -10000 /app/audit.log | grep RAG_ROUTE > /tmp/router.log

# How often is mode=cold despite a high top_score? Those are misses you
# could rescue with a slightly lower grounded threshold.
awk -F'|' '/mode=cold/ {print $4}' /tmp/router.log | \
  awk -F'=' '{ if ($2+0 > 0.5) print $2 }' | wc -l

# If that count is high (>10% of total cold decisions), drop the threshold:
kubectl set env deploy/kubeastra-backend \
  RAG_ROUTER_GROUNDED_THRESHOLD=0.65 -n k8s-devops
# Env-driven; no restart needed.

# For Phase 1.5 specifically, watch for ansible_detected=true | mode=cold —
# those are Ansible errors where deployment_repo content didn't clear the bar.
grep 'ansible_detected=true.*mode=cold' /tmp/router.log | wc -l
```

---

## Verification checklist — you're done when…

| Check | Command | Pass criterion |
|---|---|---|
| All pods Running | `kubectl get pods -n k8s-devops` | backend, frontend, qdrant-0 all `1/1 Running` |
| Qdrant ready | `kubectl exec ... qdrant-0 -- curl -s localhost:6333/readyz` | `all shards are ready` |
| Collections exist | `kubectl exec ... -- curl -s localhost:6333/collections` | JSON includes `devops_doc`, `deployment_repo` (+ `runbook` / `session_memory` after captures) |
| Doc ingestion ran | `kubectl get jobs -n k8s-devops` | Last `rag-*` job: `1/1` completed |
| Deployment repo populated | `... /collections/deployment_repo \| jq .result.points_count` | `> 4000` |
| Backend env wired | `kubectl exec ... backend -- env \| grep QDRANT_URL` | `http://kubeastra-qdrant:6333` |
| Router is firing | tail audit log, grep `RAG_ROUTE` | Mix of `mode=grounded` / `mode=cold` |
| Ansible detection works | Paste `TASK [...]` in chat; grep `ansible_detected=true` in audit | Match within seconds |
| Capture is working | `... /collections/session_memory \| jq .result.points_count` after some chats | `> 0` |
| Cached path fires | After thumbs-ups, repeat the question, grep `mode=cached` | Match |
| NetworkPolicy enforced | `kubectl run test ... -- curl qdrant:6333/readyz` from unrelated pod | Hangs/timeouts |
| CronJob scheduled | `kubectl get cronjob -n k8s-devops` | `SCHEDULE` shows `0 2 * * *`, `SUSPEND False` |

---

## The final `values.yaml` — single artifact

Here's what your `my-values.yaml` should look like with everything enabled. Drop this in alongside a `values-secrets.yaml` containing just the `secrets:` block.

```yaml
# ── my-values.yaml — full Phase 1 config ──────────────────────────────────────
namespace: k8s-devops

# ── Backend ───────────────────────────────────────────────────────────────────
backend:
  image:
    repository: ghcr.io/kubeastra/kubeastra-backend
    tag: "main-b174261"
  replicaCount: 1
  config:
    allowedNamespaces: "*"
    kubectlTimeoutSeconds: "15"
    llmProvider: "gemini"
    geminiModel: "gemini-3.1-flash-lite"
    enableRecoveryOperations: "false"          # flip to "true" once you trust the agent
    # ── RAG router ──
    ragRouterEnabled: "true"
    ragRouterTopK: "5"
    ragRouterCachedThreshold: "0.92"
    ragRouterGroundedThreshold: "0.70"
    ragRouterCollections: "runbook,devops_doc,deployment_repo"
    # ── Session capture (Phase 1.3) ──
    sessionCaptureEnabled: "true"
    sessionCaptureTtlDays: "90"
    sessionCaptureTranscriptChars: "4000"
    sessionCaptureRedactSecrets: "true"
    # ── Optional: log/event/describe summarizer (Phase 2.1) ──
    # enableLogSummarization: "true"            # uncomment for noisy clusters

# ── Frontend ──────────────────────────────────────────────────────────────────
frontend:
  image:
    repository: ghcr.io/kubeastra/kubeastra-frontend
    tag: "main-b174261"
  service:
    type: LoadBalancer

# ── Qdrant (Phase 1.1) ────────────────────────────────────────────────────────
qdrant:
  enabled: true
  image:
    repository: qdrant/qdrant
    tag: "v1.11.0"
  storage:
    size: 5Gi
    storageClassName: "standard-rwo"           # pin for prod; "" = cluster default
  resources:
    requests: { cpu: 200m, memory: 512Mi }
    limits:   { cpu: 1, memory: 2Gi }
  networkPolicy:
    enabled: true                              # production hardening

# ── RAG ingestion CronJob (Phase 1.2) ─────────────────────────────────────────
rag:
  enabled: true
  ingestion:
    schedule: "0 2 * * *"                      # nightly 02:00 cluster-local
    knowledgeVolume:
      enabled: true
      mountPath: /knowledge
      configMapName: team-runbooks             # or use a git_repo source instead
    resources:
      requests: { cpu: 200m, memory: 1Gi }
      limits:   { cpu: 1,    memory: 2Gi }

# ── Deployment repo as KB (Phase 1.5) ─────────────────────────────────────────
deploymentRepo:
  enabled: true
  url: "https://github.com/kubeastra/deployment-provisioning.git"
  branch: "main"
  subdir: "ansible"
  tokenSecretName: "deployment-repo-token"
  tokenSecretKey: "token"
```

`values-secrets.yaml` (gitignored):

```yaml
secrets:
  geminiApiKey: "AIza..."
  kubeconfig:   "<base64 of your kubeconfig>"
  mcpAuthToken: "<openssl rand -hex 32>"
```

---

## End-to-end reproducible script

```bash
#!/usr/bin/env bash
# Run from the repo root. Idempotent — re-running is safe.
set -euo pipefail
NS=k8s-devops
CHART=./helm/kubeastra

# ── Prereqs ─────────────────────────────────────────────────────────────────
kubectl version --client --short
helm version --short
kubectl get storageclass | grep -q '(default)' || \
  { echo "No default StorageClass — set qdrant.storage.storageClassName explicitly"; exit 1; }

# ── 0a. Namespace + secrets ─────────────────────────────────────────────────
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

GEMINI_KEY="${GEMINI_KEY:-${GEMINI_API_KEY:-}}"
test -n "$GEMINI_KEY" || { echo "Set GEMINI_KEY env var"; exit 1; }
KUBECONFIG_B64=$(base64 < "${KUBECONFIG:-$HOME/.kube/config}" | tr -d '\n')
MCP_TOKEN=$(openssl rand -hex 32)

cat > /tmp/values-secrets.yaml <<EOF
secrets:
  geminiApiKey: "${GEMINI_KEY}"
  kubeconfig:   "${KUBECONFIG_B64}"
  mcpAuthToken: "${MCP_TOKEN}"
EOF

# ── 0b. Team docs ConfigMap ─────────────────────────────────────────────────
mkdir -p /tmp/team-runbooks
cat > /tmp/team-runbooks/crashloop.md <<'EOF'
# CrashLoopBackOff
Pods that keep restarting usually fail their liveness probe or panic.
## Fix
- Check kubectl describe pod for Last State / Reason
- OOMKilled → bump memory limits
EOF
kubectl create configmap team-runbooks --from-file=/tmp/team-runbooks/ \
  -n "$NS" --dry-run=client -o yaml | kubectl apply -f -

# ── 0c. Deployment-repo PAT secret ──────────────────────────────────────────
test -n "${DEPLOYMENT_REPO_PAT:-}" || { echo "Set DEPLOYMENT_REPO_PAT env var"; exit 1; }
kubectl create secret generic deployment-repo-token \
  --from-literal=token="${DEPLOYMENT_REPO_PAT}" \
  -n "$NS" --dry-run=client -o yaml | kubectl apply -f -

# ── 1-5. Full values ────────────────────────────────────────────────────────
cat > /tmp/my-values.yaml <<'EOF'
namespace: k8s-devops
backend:
  image: { repository: ghcr.io/kubeastra/kubeastra-backend, tag: "main-b174261" }
  config:
    ragRouterEnabled: "true"
    ragRouterCollections: "runbook,devops_doc,deployment_repo"
    sessionCaptureEnabled: "true"
    sessionCaptureRedactSecrets: "true"
frontend:
  image: { repository: ghcr.io/kubeastra/kubeastra-frontend, tag: "main-b174261" }
qdrant:
  enabled: true
  storage: { size: 5Gi }
  networkPolicy: { enabled: true }
rag:
  enabled: true
  ingestion:
    schedule: "0 2 * * *"
    knowledgeVolume:
      enabled: true
      mountPath: /knowledge
      configMapName: team-runbooks
deploymentRepo:
  enabled: true
  url: "https://github.com/kubeastra/deployment-provisioning.git"
  branch: "main"
  subdir: "ansible"
  tokenSecretName: "deployment-repo-token"
  tokenSecretKey: "token"
EOF

# ── Install / upgrade ───────────────────────────────────────────────────────
helm upgrade --install k8s-devops "$CHART" \
  -n "$NS" -f /tmp/my-values.yaml -f /tmp/values-secrets.yaml

# ── Wait for pods ───────────────────────────────────────────────────────────
kubectl rollout status statefulset/kubeastra-qdrant -n "$NS" --timeout=180s
kubectl rollout status deploy/kubeastra-backend     -n "$NS" --timeout=120s
kubectl rollout status deploy/kubeastra-frontend    -n "$NS" --timeout=120s

# ── Bootstrap reindex (takes ~10-15 min on first run) ───────────────────────
JOB_NAME="rag-bootstrap-$(date +%s)"
kubectl create job --from=cronjob/kubeastra-rag-ingestion \
  "$JOB_NAME" -n "$NS"

echo
echo "Bootstrap job: $JOB_NAME"
echo "Follow logs with:   kubectl logs -f -n $NS job/$JOB_NAME"
echo "Then verify with:   kubectl exec -n $NS kubeastra-qdrant-0 -- \\"
echo "                      curl -s http://localhost:6333/collections | jq"
echo
echo "Frontend URL:"
kubectl get svc kubeastra-frontend -n "$NS" \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}:{.spec.ports[0].port}{"\n"}'
```

---

## Troubleshooting

### "Pod stuck in Pending" — Qdrant won't schedule

```bash
kubectl describe pod -n k8s-devops kubeastra-qdrant-0 | tail -30
```

Most likely causes:

- **No default StorageClass.** Set explicitly: `--set qdrant.storage.storageClassName=<your-sc-name>`. Find candidates with `kubectl get sc`.
- **PVC can't bind** — `kubectl get pvc -n k8s-devops` shows `Pending`. Your StorageClass may require manual provisioning (older NFS provisioners). Switch to a dynamic provisioner.
- **Node selectors / taints** — if you set `qdrant.nodeSelector`, confirm a matching node exists with `kubectl get nodes --show-labels`.

### "Pod stuck in ImagePullBackOff"

```bash
kubectl describe pod -n k8s-devops <pod> | grep -A5 Events
```

- Default chart points at `ghcr.io/kubeastra/...`. If your nodes can't reach that registry, set `backend.image.repository` / `frontend.image.repository` to a registry they CAN reach.
- For private registries, also configure `imagePullSecrets:` in values (see chart for the format).

### "INGEST_SUMMARY says new: 0 — nothing got indexed"

Two flavors:

1. **No source documents found.** The CronJob's source path doesn't match the mount path. With `knowledgeVolume.mountPath: /knowledge`, your rag config source path must be `/knowledge`. Confirm:
   ```bash
   kubectl get cm kubeastra-rag-config -n k8s-devops -o yaml | grep -A2 sources
   ```
2. **All docs were skipped as duplicates.** Re-runs are idempotent (content hash). To force a fresh ingest:
   ```bash
   kubectl exec -n k8s-devops kubeastra-qdrant-0 -- \
     curl -X DELETE http://localhost:6333/collections/devops_doc
   kubectl create job --from=cronjob/kubeastra-rag-ingestion \
     rag-reindex-$(date +%s) -n k8s-devops
   ```

### "Deployment repo bootstrap fails with `Authentication failed`"

```bash
kubectl logs -n k8s-devops job/rag-bootstrap-deployrepo | grep -A3 'clone failed'
# Token is auto-scrubbed from this log; you'll see <redacted> not the PAT.
```

- PAT lacks `Contents: Read` on the repo → re-issue with correct permissions.
- Secret key mismatch → confirm `kubectl get secret deployment-repo-token -o yaml` has a `token:` key (matches `deploymentRepo.tokenSecretKey`).
- Expired PAT → rotate: `kubectl create secret ... --dry-run=client -o yaml | kubectl apply -f -`.

### "Router decisions all say `mode=cold` with `top_score=0`"

Three possibilities:

1. **Backend can't reach Qdrant** — `kubectl exec backend-pod -- curl -s qdrant:6333/readyz`. If it fails, check NetworkPolicy isn't over-restrictive.
2. **Collection empty** — `kubectl exec qdrant-0 -- curl -s localhost:6333/collections/devops_doc | jq '.result.points_count'` should be > 0.
3. **Embedding model mismatch** — if you changed `embeddingModel` without also changing `embeddingDim`, search vectors don't match indexed ones. Delete + reindex.

### "Router decisions show high `top_score` but `mode=cold`"

Your `RAG_ROUTER_GROUNDED_THRESHOLD` (default 0.70) is above the typical similarity in your corpus. Lower it:

```bash
kubectl set env deploy/kubeastra-backend \
  RAG_ROUTER_GROUNDED_THRESHOLD=0.60 -n k8s-devops
# Env-driven; no restart needed (pydantic-settings re-reads on next request).
```

### "NetworkPolicy blocks the backend from reaching Qdrant"

After enabling `qdrant.networkPolicy.enabled=true`, the backend's chats start failing with `vector DB unavailable`.

```bash
# Confirm the policy's spec.ingress.from matches your backend pod labels
kubectl get networkpolicy -n k8s-devops -o yaml | grep -A20 ingress

# Confirm the backend pods carry app.kubernetes.io/component=backend
kubectl get pod -n k8s-devops -l app.kubernetes.io/component=backend
```

If labels don't match, the chart's selector is stale. Pin to a known-good chart version or report the mismatch.

### "Chat works locally but RAG decision never shows in response"

The `rag_decision` field is on the **sync** `/api/chat` endpoint. The **streaming** `/api/chat/stream` endpoint emits a separate `kb_route` SSE event. If you're testing via the streaming endpoint:

```bash
curl -N -sS -X POST http://localhost:8000/api/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"message":"...","history":[]}' | head -20
# Look for: event: kb_route
#           data: {"mode":"grounded",...}
```

---

## Cost / footprint reality check

| Concern | Reality |
|---|---|
| **Qdrant storage** | A team's KB rarely exceeds 50 MB. The default 5 Gi PVC is ~100× more than needed. |
| **Qdrant CPU/mem** | Default 200m CPU / 512 Mi mem requests handle tens of thousands of vectors fine. |
| **Embedding cost** | Local MiniLM, CPU-only, free. Re-ingesting unchanged docs is a no-op (content-hash idempotency). |
| **First reindex** | ~15 min for deployment-repo (4 k chunks). Subsequent: ~1 min (incremental). |
| **LLM bill** | Goes **down** when cached/grounded kicks in. Cached = zero tokens; grounded = similar to cold but better quality. |
| **What if Qdrant goes down?** | Chat keeps working. Router falls back to `cold` mode. Warnings in backend logs. Nothing data-destructive. |
| **What if deployment repo PAT expires?** | Next reindex fails silently (CronJob marked failed); existing chunks stay searchable. Rotate the secret and the next run recovers. |

---

## Related docs

In this repo:
- [K8S_DEPLOYMENT_GUIDE.md](K8S_DEPLOYMENT_GUIDE.md) — general chart deployment notes
- [ARCHITECTURE_DIAGRAM.md](ARCHITECTURE_DIAGRAM.md) — repo-level component diagram

Internal-only (not shipped — kept in `docs/internal_docs/` for contributors):
- `QDRANT_DEPLOYMENT_GUIDE.md` — full operator reference: every knob, rollback matrix, scenarios A/B/C (laptop / staging / production), the auth-wiring-gap caveat
- `DEPLOYMENT_REPO_KB_USER_GUIDE.md` — Phase 1.5 in depth
- `DEPLOYMENT_REPO_KB_PLAN.md` — Phase 1.5 design + runtime model
- `AGENT_FEATURE_ROADMAP.md` — what's shipped, what's next
- `HARDENING_RECOMMENDATIONS.md` — production-readiness checklist
