# KubeAstra Assistant — Kubernetes Deployment Guide

This guide deploys the full KubeAstra stack onto a Kubernetes cluster using the
Helm chart at `helm/kubeastra/`.

**Most people do not need to build anything.** Published images are public:

```bash
helm install kubeastra ./helm/kubeastra --namespace kubeastra --create-namespace
```

The chart defaults to `ghcr.io/astraverse-io/kubeastra-{backend,frontend}:latest`,
which anyone can pull without authenticating. Skip to
[Step 4](#step-4--prepare-the-kubeconfig-secret) unless you need your own images.

Build your own only if your cluster cannot reach ghcr.io, you must mirror into
an internal registry, or you are deploying local changes — Steps 1–3 cover that.

> **Paths** in this guide are relative to the repository root.

---

## Architecture deployed

```
Container registry
  ├── kubeastra-backend:main    ← FastAPI :8000 + HTTP MCP :8001 + mcp
  └── kubeastra-frontend:main   ← Next.js standalone

  Published tags: `latest` and `<version>` on each release, plus `main`
  (every merge) and `sha-<short>` (a fixed commit).

Kubernetes namespace: kubeastra
  ├── Deployment/backend                       (FastAPI :8000 + HTTP MCP :8001)
  ├── Deployment/frontend                      (Next.js :3000, server-side proxy)
  ├── Service/backend                          (ClusterIP :8000)
  ├── Service/mcp                              (ClusterIP :8001 — external MCP surface)
  ├── Service/frontend                         (ClusterIP :3000)
  ├── StatefulSet/qdrant + Service             (Qdrant v1.11.x for RAG)
  ├── PVC/qdrant-data                          (Qdrant vector storage)
  ├── PVC/chat-history                         (SQLite — optional but recommended)
  ├── NetworkPolicy/qdrant                     (backend → qdrant ingress)
  ├── Job/rag-bootstrap                        (post-install/upgrade hook — first reindex)
  ├── CronJob/rag-ingestion                    (periodic reindex from configured sources)
  ├── ConfigMap/app-config                     (RAG flags, triage flags, capture knobs)
  ├── ConfigMap/kb-config                      (KB_CONFIG_YAML — ingestion source list)
  ├── Secret/app-secrets                       (provider API key(s) + kubeconfig)
  ├── Secret/deployment-repo-token             (PAT for the deployment repo, Phase 1.5)
  ├── ServiceAccount + ClusterRole + Binding   (pod identity, kubectl perms)
  └── Ingress                                  (optional — disabled by default)
```

### Key features in the deployed stack

| Feature | How it works in K8s |
|---|---|
| Chat UI | Next.js frontend proxies `/api/*` → FastAPI backend. SSE streaming via `/api/chat/stream` |
| LLM provider | Gemini, Anthropic or OpenAI — the chart templates `GEMINI_API_KEY`, `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` from the Secret; set whichever you use. Ollama is supported by the app for local models |
| Local kubectl | kubeconfig Secret mounted at `/app/kubeconfig/config` |
| SSH remote cluster | Users provide SSH creds in the UI at runtime — no extra K8s config |
| RAG (Phase 1.x) | Qdrant StatefulSet ships with the chart; collections bootstrapped at pod startup; ingestion runs on Helm hooks + a CronJob |
| Deployment-repo KB (Phase 1.5) | Ansible-aware chunking; auth via `deploymentRepo.token` (PAT). See [BEST_FEATURES_QUICKSTART.md](BEST_FEATURES_QUICKSTART.md) |
| Proactive cluster triage (Phase 3.0) | First chat of a session emits a `triage_greet` SSE event surfacing CrashLoop/Pending/Warning state. Flag: `enableProactiveTriage` |
| Conversation memory (Phase 2.2) | Per-session "you've been working on X" prefix injected into prompts. SQLite-backed |
| Semantic prompt cache (Phase 2.3) | Tight-threshold match against recent `session_memory` → instant cached answer, zero LLM calls |
| Session capture flywheel (Phase 1.3 + 1.4) | Worthy chats → `session_memory` (90d TTL). User 👍 → promoted to `runbook` (verified, no TTL) |
| HTTP MCP server | Started by the same entrypoint as FastAPI; reachable at `Service/mcp:8001/mcp/` for external MCP clients |
| SQLite persistence | `chat_history.db` at `/app/data/` — PVC required for durable chat history, session memory, cluster selections, and feedback audit records |

---

## Prerequisites

| Tool | Minimum version | Check |
|---|---|---|
| Docker | 20.x+ | `docker --version` |
| kubectl | 1.26+ | `kubectl version --client` |
| Helm | 3.12+ | `helm version` |
| Access to target K8s cluster | — | `kubectl cluster-info` |
| A container registry | only if building your own images | `docker login <your-registry>` |

Docker itself is only needed for Steps 1–3. Deploying the published images
needs just kubectl and Helm.

---

## Step 1 — Build the backend Docker image

> Only needed if you are not using the published images. See the top of this
> guide.

The backend image bundles both `ui/backend` and `mcp` into a single image. The
build context **must be the repository root** so Docker can COPY both
subdirectories.

```bash
cd /path/to/KubeAstra

# `sha-<short>` matches the tag scheme the project publishes.
REGISTRY=your-registry.example.com
SHA=$(git rev-parse --short HEAD)
docker build \
  -f ui/backend/Dockerfile \
  -t ${REGISTRY}/kubeastra-backend:sha-${SHA} \
  .
```

> **Note:** `.github/workflows/release.yml` does this on every merge to `main`,
> so the command above is for one-off or local-change builds.
>
> **Build from a clean tree.** `COPY ui/backend/ /app/` copies a whole
> directory. `.dockerignore` excludes `.env` and `*.db`, but anything else
> uncommitted in your checkout lands in the image — build from a fresh clone or
> `git archive` if you want the image to match the commit.

**What this image contains:**
- Python 3.11-slim base
- `kubectl` binary (downloaded at build time)
- All Python dependencies from both `mcp/requirements.txt` and `ui/backend/requirements.txt` (paramiko for SSH, qdrant-client pinned to match server, sentence-transformers + torch CPU for embeddings, google-genai for Gemini)
- `mcp/` source at `/app/mcp/`
- `ui/backend/` source at `/app/`
- `entrypoint.sh` starts **two processes**: FastAPI on `:8000` (chat) and the HTTP MCP server on `:8001` (external MCP clients)
- `KUBECONFIG=/app/kubeconfig/config` (mounted as a Secret at runtime)
- `HF_HOME=/tmp/hf-cache` so the sentence-transformer model survives across requests on a read-only rootfs
- SQLite database written to `/app/data/chat_history.db` at runtime
- An import + annotation-resolution check baked into the Dockerfile as a build step — a missing import in `react.py` / `triage.py` fails the image build rather than production. `ui/backend/tests/test_module_imports.py` is the fuller local equivalent

**Verify the build:**
```bash
docker run --rm ${REGISTRY}/kubeastra-backend:sha-${SHA} python -c "import fastapi, paramiko, qdrant_client, sentence_transformers; print('OK')"
```

---

## Step 2 — Build the frontend Docker image

The frontend includes a **server-side proxy** at `app/api/[...path]/route.ts`.

- the browser talks to the frontend at `/api/*`
- the Next.js server proxies those requests to the backend
- the backend target is configured at runtime with `API_BASE_URL`
- you do **not** need to rebuild the frontend image just to change the backend URL

```bash
cd /path/to/KubeAstra/ui/frontend

# Same tag scheme as the backend.
docker build \
  -t ${REGISTRY}/kubeastra-frontend:sha-${SHA} \
  .
```

At runtime, the frontend container reads:

```bash
API_BASE_URL=http://<backend-host>:8000
```

from its environment.

**Verify the build:**
```bash
docker run --rm -p 3000:3000 ${REGISTRY}/kubeastra-frontend:sha-${SHA}
# Open http://localhost:3000 — you should see the chat UI
```

---

## Step 3 — Push images to your registry

```bash
docker login ${REGISTRY}

docker push ${REGISTRY}/kubeastra-backend:sha-${SHA}
docker push ${REGISTRY}/kubeastra-frontend:sha-${SHA}
```

The published ghcr images are public and need no pull secret. If your own
registry is private:

```bash
kubectl create secret docker-registry registry-pull-secret \
  --namespace kubeastra \
  --docker-server=${REGISTRY} \
  --docker-username=YOUR_USERNAME \
  --docker-password=YOUR_PASSWORD \
  --docker-email=YOUR_EMAIL
```

Then add to `values.yaml`:
```yaml
imagePullSecrets:
  - name: registry-pull-secret
```

---

## Step 4 — Prepare the kubeconfig Secret

The backend pod runs `kubectl` as a subprocess and needs a kubeconfig to authenticate to your cluster. You provide this via a Kubernetes Secret that is volume-mounted into the pod at `/app/kubeconfig/config`.

### Option A — Use your local kubeconfig (simplest)

```bash
# Base64-encode your kubeconfig (single line, no newlines)
cat ~/.kube/config | base64 | tr -d '\n'
```

Copy the output — you will pass it to Helm in Step 5.

### Option B — Create a dedicated service account kubeconfig (recommended for production)

This creates a minimal kubeconfig with only the permissions the app needs:

```bash
# 1. Create a service account in the TARGET cluster the app will query
kubectl create serviceaccount kubeastra-app -n kube-system

# 2. Create a ClusterRoleBinding for it (reuse the role from the Helm chart)
kubectl create clusterrolebinding kubeastra-app \
  --clusterrole=cluster-reader \
  --serviceaccount=kube-system:kubeastra-app

# 3. Create a long-lived token (K8s 1.24+)
kubectl create token kubeastra-app -n kube-system --duration=8760h > /tmp/kubeastra-token.txt

# 4. Build a minimal kubeconfig using the token
CLUSTER_SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
CLUSTER_CA=$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
TOKEN=$(cat /tmp/kubeastra-token.txt)

cat > /tmp/kubeastra-kubeconfig.yaml << EOF
apiVersion: v1
kind: Config
clusters:
- cluster:
    server: ${CLUSTER_SERVER}
    certificate-authority-data: ${CLUSTER_CA}
  name: target-cluster
contexts:
- context:
    cluster: target-cluster
    user: kubeastra-app
  name: kubeastra
current-context: kubeastra
users:
- name: kubeastra-app
  user:
    token: ${TOKEN}
EOF

# 5. Base64-encode it
cat /tmp/kubeastra-kubeconfig.yaml | base64 | tr -d '\n'
```

---

## Step 5 — Create the namespace and install the Helm chart

```bash
# Navigate to the Helm chart directory
cd /path/to/KubeAstra/helm/kubeastra

# Dry-run first to check everything renders correctly
helm install kubeastra . \
  --namespace kubeastra \
  --create-namespace \
  --dry-run \
  --set backend.image.repository=${REGISTRY}/kubeastra-backend \
  --set backend.image.tag=1.0.0 \
  --set frontend.image.repository=${REGISTRY}/kubeastra-frontend \
  --set frontend.image.tag=1.0.0 \
  --set secrets.geminiApiKey="YOUR_GEMINI_API_KEY" \
  --set secrets.kubeconfig="PASTE_BASE64_KUBECONFIG_HERE"
```

If the dry-run output looks correct, install for real:

```bash
helm install kubeastra . \
  --namespace kubeastra \
  --create-namespace \
  --set backend.image.repository=${REGISTRY}/kubeastra-backend \
  --set backend.image.tag=main-${SHA} \
  --set frontend.image.repository=${REGISTRY}/kubeastra-frontend \
  --set frontend.image.tag=main-${SHA} \
  --set secrets.geminiApiKey="YOUR_GEMINI_API_KEY" \
  --set secrets.kubeconfig="PASTE_BASE64_KUBECONFIG_HERE"
```

### Alternative — use a values override file (recommended, keeps secrets out of shell history)

Create `my-values.yaml` (do not commit this file):

```yaml
backend:
  image:
    repository: ${REGISTRY}/kubeastra-backend
    tag: "main-abcdef0"      # set to the SHA you actually built

frontend:
  image:
    repository: ${REGISTRY}/kubeastra-frontend
    tag: "main-abcdef0"

secrets:
  geminiApiKey: "YOUR_GEMINI_API_KEY"
  kubeconfig: "PASTE_BASE64_KUBECONFIG_HERE"

# Phase 1.x — RAG (Qdrant ships with the chart by default; nothing to flip on)
# Phase 1.5 — deployment-repo KB (optional; needs a fine-grained PAT)
deploymentRepo:
  enabled: true
  url: "https://github.com/your-org/your-deployment-repo.git"
  branch: "main"
  token: "ghp_xxxxxxxxxxxxxxxxxxxx"   # or omit and create the Secret separately

# Phase 3.0 — proactive cluster triage on first chat
enableProactiveTriage: true
proactiveTriageNamespaces: "*"
proactiveTriageEventLookbackMin: 10

# Phase 1.3 — session capture flywheel (enabled by default)
sessionCaptureEnabled: true
sessionCaptureTtlDays: 90
sessionCaptureRedactSecrets: true

# Feature flags
useReactChat: false          # set true to make /api/chat (sync) default to ReAct
```

For the full operator playbook (every knob, hardening, the deployment-repo KB walk-through), see [BEST_FEATURES_QUICKSTART.md](BEST_FEATURES_QUICKSTART.md).

Then install with:

```bash
helm install kubeastra . \
  --namespace kubeastra \
  -f my-values.yaml
```

---

## Step 5b — Multi-environment deployment (DEV/PROD)

If you deploy to **multiple environments** (e.g. `k8s-ai-test` for DEV and
`kubeastra` for PROD), the chart resolves several per-env knobs
automatically from `.Release.Namespace`, so you don't need a
`values-dev.yaml`/`values-prod.yaml` pair. Just change `--namespace`.

### What auto-resolves by namespace

These three template helpers in `helm/kubeastra/templates/_helpers.tpl`
each accept overrides but default-pick by namespace:

| Helper | What it picks | Configured in `values.yaml` as |
|---|---|---|
| `loadBalancerIP` | The pre-reserved static internal IP for both frontend (`:3000`) and backend (`:8000`) Services | `networking.loadBalancerIPByNamespace` map |
| `backendServiceType` | `LoadBalancer` (with the GCP Internal LB annotation merged in) when the namespace is in the IP map; `ClusterIP` otherwise | `backend.service.type` (leave empty to infer) |
| `alertWebhookToken` | The Alertmanager bearer token | `secrets.alertWebhookToken_Dev`, `secrets.alertWebhookToken_Prod`, or `secrets.alertWebhookTokensByNamespace` map |

### Example: dev/prod with one `values-secrets.yaml`

```yaml
# values.yaml (committed)
networking:
  loadBalancerIPByNamespace:
    k8s-ai-test: "10.0.0.100"   # DEV — kubeastra-dev.example.com
    kubeastra:  "10.0.0.101"   # PROD — kubeastra-prod.example.com

backend:
  service:
    type: ""   # empty = inferred from namespace; set to "ClusterIP" to opt out

# values-secrets.yaml (gitignored)
secrets:
  geminiApiKey:           "<...>"
  alertWebhookToken_Dev:  "<DEV TOKEN, see docs/alert_manager_deploy.md>"
  alertWebhookToken_Prod: "<PROD TOKEN>"
```

Then the install command is identical between environments — only the
namespace changes:

```bash
# DEV
helm upgrade --install kubeastra . \
  --namespace k8s-ai-test \
  -f values.yaml -f values-secrets.yaml

# PROD
helm upgrade --install kubeastra . \
  --namespace kubeastra \
  -f values.yaml -f values-secrets.yaml
```

After install, `kubectl get svc -n <ns>` shows both frontend and backend
sharing the same EXTERNAL-IP. The webhook is reachable at e.g.
`http://kubeastra-dev.example.com:8000/api/v1/alerts/webhook`.

### Shared-ILB prerequisite (GCP)

For two Services to share one internal IP, the address must be reserved
with `--purpose=SHARED_LOADBALANCER_VIP`:

```bash
gcloud compute addresses create cortex-dev \
  --region=<region> --subnet=<subnet> \
  --addresses=10.0.0.100 \
  --purpose=SHARED_LOADBALANCER_VIP
```

If reserved with the default `GCE_ENDPOINT` purpose, the first Service
(frontend) claims the IP and the second (backend) stays in `<pending>`.

### Alertmanager wiring + ops

For the Alertmanager webhook setup (token rotation, AM-side Secret
mounting, RCA depth tuning, threading/concurrency knobs), see
[alert_manager_deploy.md](alert_manager_deploy.md). It's the canonical
reference for the alerts/RCA subsystem; this guide covers only the
helm-install side.

---

## Step 6 — Verify the deployment

```bash
# Check all pods are Running
kubectl get pods -n kubeastra

# Expected output:
# NAME                                         READY   STATUS    RESTARTS   AGE
# kubeastra-kubeastra-backend-...  1/1     Running   0          60s
# kubeastra-kubeastra-frontend-... 1/1     Running   0          60s

# Check services
kubectl get services -n kubeastra

# Check backend logs
kubectl logs -n kubeastra deployment/kubeastra-kubeastra-backend --follow

# Verify kubectl works inside the backend pod
kubectl exec -n kubeastra \
  deployment/kubeastra-kubeastra-backend \
  -- kubectl get nodes
```

---

## Step 7 — Access the UI

### Option A — Port-forward (quickest, no Ingress needed)

Open two terminal windows:

```bash
# Terminal 1 — backend
kubectl port-forward -n kubeastra service/kubeastra-kubeastra-backend 8000:8000

# Terminal 2 — frontend
kubectl port-forward -n kubeastra service/kubeastra-kubeastra-frontend 3000:3000
```

Open `http://localhost:3000` in your browser.

The browser talks to the frontend on port `3000`, and the frontend server proxies `/api/*` to the backend on port `8000`.

### Option B — Ingress (for team access)

Enable Ingress in your values and upgrade:

```bash
helm upgrade kubeastra . \
  --namespace kubeastra \
  -f my-values.yaml \
  --set ingress.enabled=true \
  --set ingress.frontendHost=kubeastra.your-company.com \
  --set ingress.backendHost=kubeastra-api.your-company.com \
  --set ingress.className=nginx
```

> **Runtime config note:** With the new proxy model, switching backend targets is usually a frontend runtime env change (`API_BASE_URL`), not a frontend rebuild.

---

## Step 8 — Upgrading after a code change

```bash
# Run from the repository root
# 1. Rebuild and push images with a new tag
docker build -f ui/backend/Dockerfile -t ${REGISTRY}/kubeastra-backend:1.0.1 .
docker push ${REGISTRY}/kubeastra-backend:1.0.1

# 2. Upgrade the Helm release with the new image tag
cd helm/kubeastra
helm upgrade kubeastra . \
  --namespace kubeastra \
  -f my-values.yaml \
  --set backend.image.tag=1.0.1
```

---

## Local development — start.sh

For local development, use the provided `start.sh` script (no Docker or Helm needed):

```bash
cd ui
./start.sh
```

This starts:
- **Backend** — `uvicorn main:app --port 8000` (with `MCP_PATH` and `PYTHONPATH` set to `mcp/`)
- **Frontend** — `npm run dev` on port 3000 with `API_BASE_URL=http://localhost:8000`

Press `Ctrl+C` to stop both.

> **Note:** The backend writes `chat_history.db` to `ui/backend/` locally. This file is git-ignored.

---

## SQLite persistence in Kubernetes

The backend automatically creates `chat_history.db` at startup (path: `/app/data/chat_history.db` inside the container). This SQLite file stores chat history, per-session memory, selected cluster connections, remembered SSH targets, and feedback audit records. Without a persistent volume this file is lost when the pod restarts.

**To persist chat history and feedback audit records across pod restarts**, add a PVC to your `my-values.yaml`:

```yaml
backend:
  persistence:
    enabled: true
    storageClass: "standard"   # use your cluster's storage class
    size: 1Gi
    mountPath: /app/data
```

If `persistence.enabled` is false (default), the backend still works — users lose history, session memory, cluster selections, and feedback audit rows on pod restart. `values-production.yaml` enables this PVC by default.

---

## Complete file structure

```
KubeAstra/
├── docs/
│   ├── ARCHITECTURE_DIAGRAM.md          ← Repo-level mermaid diagrams + component table
│   ├── K8S_DEPLOYMENT_GUIDE.md          ← This file
│   ├── BEST_FEATURES_QUICKSTART.md      ← End-to-end operator playbook
│   └── internal_docs/                   ← Internal planning, gitignored
├── ui/
│   ├── start.sh                         ← Local dev launcher (backend + frontend)
│   ├── backend/
│   │   ├── Dockerfile                   ← Bundles mcp + backend + entrypoint.sh
│   │   ├── entrypoint.sh                ← Starts FastAPI :8000 AND HTTP MCP :8001
│   │   ├── main.py                      ← FastAPI app + lifespan (DB init + RAG bootstrap + embed warmup)
│   │   ├── react.py                     ← ReAct loop (think → act → observe, SSE-streamed)
│   │   ├── triage.py                    ← Phase 3.0 proactive cluster greeting
│   │   ├── memory.py                    ← Phase 2.2 per-user conversation memory
│   │   ├── db.py                        ← SQLite schema + CRUD
│   │   ├── tests/test_module_imports.py ← Import smoke tests (local; the image runs an inline equivalent)
│   │   └── routers/
│   │       ├── chat.py                  ← /api/chat (sync) + /api/chat/stream (SSE)
│   │       ├── sessions.py              ← History, SSH target, post-mortem generator
│   │       ├── models.py                ← LLM model catalog (dynamic discovery)
│   │       ├── feedback.py              ← Thumbs-up → promotion to runbook
│   │       ├── cluster.py               ← Cluster health + context
│   │       ├── ai_tools.py / kubectl.py / recovery.py / health.py
│   └── frontend/
│       ├── Dockerfile                   ← Next.js standalone build
│       ├── next.config.ts               ← output: 'standalone' + memory optimizations
│       ├── app/api/[...path]/route.ts   ← Server-side proxy → backend
│       └── components/                  ← IntentBar (Stop button), ResultView, etc.
├── mcp/
│   ├── tool_registry.py                 ← Single source of truth for all 48 tools
│   ├── http_server.py                   ← HTTP MCP server (port :8001)
│   ├── k8s/
│   │   ├── wrappers.py                  ← 41 high-level kubectl workflows
│   │   ├── kubectl_runner.py            ← Local kubectl (kubeconfig)
│   │   ├── ssh_runner.py                ← Remote kubectl via SSH (paramiko)
│   │   ├── parsers.py / validators.py
│   ├── ai_tools/                        ← analyze, fix, runbook, report
│   └── services/
│       ├── llm/                         ← base + gemini_provider + ollama_provider
│       ├── rag/                         ← router, prompt_cache, capture, promotion,
│       │                                  ingestion, chunking, chunking_ansible,
│       │                                  schema, sources/{local_path,git_repo}.py
│       ├── vector_db.py                 ← Qdrant client
│       ├── embeddings.py                ← sentence-transformers (all-MiniLM-L6-v2)
│       ├── summarizer/ / plans.py / confirmation.py
└── helm/
    └── kubeastra/
        ├── Chart.yaml
        ├── values.yaml                  ← All configurable parameters
        ├── values-production.yaml       ← Production overrides
        └── templates/
            ├── _helpers.tpl
            ├── backend-deployment.yaml  ← Backend + HTTP MCP in one pod
            ├── backend-service.yaml     ← ClusterIP :8000
            ├── mcp-service.yaml         ← ClusterIP :8001 (external MCP surface)
            ├── frontend-deployment.yaml ← Passes API_BASE_URL at runtime
            ├── frontend-service.yaml    ← ClusterIP :3000
            ├── qdrant-statefulset.yaml  ← Qdrant v1.11.x + PVC
            ├── qdrant-service.yaml
            ├── qdrant-networkpolicy.yaml ← backend → qdrant ingress
            ├── rag-bootstrap-job.yaml   ← Post-install/upgrade hook (first reindex)
            ├── rag-ingestion-cronjob.yaml ← Periodic reindex
            ├── deployment-repo-token-secret.yaml ← PAT for Phase 1.5
            ├── configmap.yaml           ← Non-secret env (incl. KB_CONFIG_YAML)
            ├── secret.yaml              ← provider API key(s) + kubeconfig
            ├── serviceaccount.yaml
            ├── rbac.yaml
            ├── pvc.yaml                 ← Optional chat_history.db PVC
            └── ingress.yaml             ← Optional, disabled by default
```

---

## Troubleshooting

### Backend pod is stuck in Init state

The init container runs `kubectl config view` to verify the kubeconfig is readable. If it fails:

```bash
# Check init container logs
kubectl logs -n kubeastra \
  $(kubectl get pod -n kubeastra -l app.kubernetes.io/component=backend -o name) \
  -c kubeconfig-check

# Verify the Secret was created with the kubeconfig key
kubectl get secret -n kubeastra kubeastra-kubeastra-secrets -o yaml
```

Common causes:
- Base64 encoding has newlines — re-encode with `| tr -d '\n'`
- kubeconfig references a cluster unreachable from inside the pod (e.g., `localhost`)
- Kubeconfig uses exec-based auth (GKE workload identity) that doesn't work in a container — use token-based auth (Option B in Step 4)

### Backend pod starts but kubectl commands fail

```bash
# Shell into the backend pod
kubectl exec -it -n kubeastra \
  deployment/kubeastra-kubeastra-backend \
  -- bash

# Inside the pod:
echo $KUBECONFIG          # Should be /app/kubeconfig/config
cat $KUBECONFIG           # Should show your kubeconfig YAML
kubectl get nodes         # Test connectivity
kubectl get pods -A       # Test namespace access
```

### LLM features not working (kubectl tools still work)

```bash
# Check the secret is set
kubectl exec -n kubeastra \
  deployment/kubeastra-kubeastra-backend \
  -- env | grep -E 'GEMINI|ANTHROPIC|OPENAI'

# If empty, update the secret — substitute the key for the provider you use
kubectl patch secret kubeastra-kubeastra-secrets \
  -n kubeastra \
  --type='json' \
  -p='[{"op":"replace","path":"/data/GEMINI_API_KEY","value":"'$(echo -n "YOUR_KEY" | base64)'"}]'

# Restart the backend pod to pick up the new secret
kubectl rollout restart deployment/kubeastra-kubeastra-backend -n kubeastra
```

### SSH cluster connection fails

SSH remote cluster support uses `paramiko` (already in `backend/requirements.txt`). No extra K8s config is needed — users provide hostname/username/password through the UI at runtime. If SSH fails:

```bash
# Verify paramiko is installed inside the backend pod
kubectl exec -n kubeastra \
  deployment/kubeastra-kubeastra-backend \
  -- python -c "import paramiko; print(paramiko.__version__)"

# Check backend logs for SSH errors
kubectl logs -n kubeastra deployment/kubeastra-kubeastra-backend | grep -i ssh
```

Common causes:
- Target host not reachable from inside the K8s cluster (firewall/VPN rules)
- Wrong SSH port (default 22)
- Username has no `kubectl` access on the remote node

---

### Frontend shows "Failed to fetch" or blank results

This means the frontend server cannot reach the backend target or the browser cannot reach the frontend.

```bash
# Check the runtime backend target inside the frontend container
kubectl exec -n kubeastra \
  deployment/kubeastra-kubeastra-frontend \
  -- env | grep API_BASE_URL

# Check frontend logs
kubectl logs -n kubeastra deployment/kubeastra-kubeastra-frontend --follow
```

Common causes:
- `API_BASE_URL` points to the wrong backend service or host
- backend Service name or port is wrong
- frontend is reachable but backend pod is failing readiness/liveness
- Ingress or port-forward only exposes frontend, while backend is unavailable behind the proxy

### Qdrant client/server version mismatch in backend logs

```
UserWarning: Qdrant client version 1.18.0 is incompatible with server version 1.11.0.
```

The Python client is pinned to match the Qdrant server's minor version in `mcp/requirements.txt` (currently `qdrant-client>=1.11.0,<1.12.0`). If you see this warning, your image was built before the pin landed. Rebuild and redeploy.

If you intentionally want to bump Qdrant: update the StatefulSet image tag AND the `requirements.txt` pin in the same commit, then rebuild.

### `Vector search in <collection> failed: 404 Not Found`

The collection was queried before it existed. Fixed by the lifespan bootstrap in `main.py:_bootstrap_rag_collections` which calls `ensure_collection_for` on every known collection at pod startup. If you see this anyway:

```bash
# Confirm bootstrap ran (line should appear once near pod start)
kubectl logs -n kubeastra -l app.kubernetes.io/component=backend | grep "RAG bootstrap"

# Check Qdrant directly from inside the backend pod
kubectl exec -n kubeastra -l app.kubernetes.io/component=backend -c backend -- \
  python3 -c "import httpx,os; r=httpx.get(os.environ['QDRANT_URL']+'/collections', timeout=5); print(r.json())"
```

If the bootstrap log line is missing, the backend either failed to import the RAG modules at startup or Qdrant was unreachable. Both surface as `RAG bootstrap: ...` warning lines.

### Chat response cuts off mid-sentence

The `max_tokens` cap on the chat finalize stream was raised from 2500 → 8000 (Gemini 2.5 Flash output ceiling). If you're still seeing chops:

1. Check the `finish_reason` on the SSE `done` event — if it says `MAX_TOKENS`, you're hitting the cap. Bump again or trim context.
2. If `finish_reason` says `STOP` but the text still looks chopped, the cap isn't the issue — check the TCP/load-balancer idle timeout.
3. Make sure `GEMINI_MODEL` resolves to the fixed chat model (`gemini-3.1-flash-lite`) and that your API key has access to it.

### GKE NetworkPolicy blocks backend → Qdrant on Dataplane V2

GKE's Dataplane V2 enforces NetworkPolicy stricter than the typical Calico setup. If `backend` can't reach `qdrant` even though the policy looks right:

```bash
# Confirm both pods are running and have the expected labels
kubectl get pods -n kubeastra --show-labels | grep -E "backend|qdrant"

# Check the policy
kubectl describe networkpolicy -n kubeastra qdrant
```

If a namespaceSelector fix doesn't work on your cluster, disable the policy temporarily and rely on Service-level controls until you can debug Dataplane V2 specifics:
```bash
helm upgrade kubeastra . -f my-values.yaml --set qdrant.networkPolicy.enabled=false
```

### HuggingFace unauthenticated warning

```
Warning: You are sending unauthenticated requests to the HF Hub. Please set a HF_TOKEN ...
```

Cosmetic. The sentence-transformer model is cached in `/tmp/hf-cache` after the first download, so this only fires on a cold pod and only matters if you actually hit HF's anonymous rate limit (1 download per IP per hour for popular models). To silence: create a free HF account, generate a read-only token, add it to the secret as `HF_TOKEN`.

### Checking the Helm release status

```bash
helm status kubeastra -n kubeastra
helm get values kubeastra -n kubeastra
```

### Uninstalling

```bash
helm uninstall kubeastra -n kubeastra
kubectl delete namespace kubeastra
```

---

## RAG / Qdrant

The chart ships a **Qdrant StatefulSet** by default (Phase 1.1+). No extra step needed — it's installed alongside the backend. The backend's `QDRANT_URL` is auto-derived from the in-cluster service name.

> **Earlier docs referenced Weaviate.** Weaviate was used pre-Phase-1.1 and is no longer supported anywhere in the codebase. The Helm chart, the backend, and the ingestion jobs all assume Qdrant.

**Disabling the chart-managed Qdrant** (e.g. you have a shared Qdrant elsewhere):

```bash
helm upgrade kubeastra helm/kubeastra \
  --namespace kubeastra \
  -f my-values.yaml \
  --set qdrant.enabled=false \
  --set qdrant.externalUrl=http://your-qdrant.example:6333
```

**Version pinning matters.** The Python `qdrant-client` is pinned to match the deployed server's minor version (currently `>=1.11.0,<1.12.0` against Qdrant `v1.11.x`). The client emits a hard warning and risks silent API drift when minors diverge by more than 1. When bumping Qdrant's image tag in the StatefulSet, bump `mcp/requirements.txt` in the same commit.

**Bootstrap is automatic.** On every backend pod start, the FastAPI lifespan hook calls `ensure_collection_for` on `runbook`, `devops_doc`, `deployment_repo`, and `session_memory`, then runs one throwaway `embeddings.embed("warmup")` so the first chat doesn't pay the 5-10s sentence-transformer load. All wrapped in try/except — the pod still boots if Qdrant is unreachable.

For the full ingestion walk-through, deployment-repo KB enablement, and tuning knobs, see [BEST_FEATURES_QUICKSTART.md](BEST_FEATURES_QUICKSTART.md).
## Security rollout notes

### Pre-deployment parity checklist

Before deploying any of the security-related phases (cookies, NetworkPolicies),
capture and compare the following values between staging and production. The
chart cannot detect a mismatch — wrong values here will surface as 503s,
broken probes, or blocked traffic after enforcement.

| Item | Where to check | Why it matters |
|---|---|---|
| Backend pod UID/GID | `kubectl exec backend -- id` | Audit log writes require UID 1000 with `fsGroup: 1000`. |
| `/app/data` volume mount | `kubectl describe pod backend` → Volumes | Must be writable; PVC if `persistence.enabled=true`, else `emptyDir`. |
| `persistence.enabled` | Helm release values | Off in staging + on in prod is a common drift source for SQLite state. |
| Ingress controller namespace and pod labels | `kubectl get pods -n <ingress-ns> -L app.kubernetes.io/name` | Required so the frontend ingress NetworkPolicy actually matches; mismatches silently drop traffic. |
| Load-balancer health-check source ranges | Cloud-provider docs (GCP/AWS/Azure) | Add to `networkPolicy.ingressCidrs` or readiness probes will fail. |
| Kubernetes API endpoint IP/CIDR | `kubectl get endpoints kubernetes -n default -o yaml` | Goes into `networkPolicy.kubernetesApiCidrs`; without it the in-cluster `kubectl version` health check fails. |
| DNS resolver namespace/labels | `kubectl get pods -n kube-system -l k8s-app=kube-dns` | `networkPolicy.dns` must match or all egress breaks once default-deny is on. |
| Qdrant and RAG-ingestion pod labels | `kubectl get pods -l app.kubernetes.io/component=qdrant` | Backend → Qdrant egress rule keys off these. |
| Egress path to Google APIs | `kubectl exec backend -- curl -v https://generativelanguage.googleapis.com` | Direct, Private Google Access, or corporate proxy — each requires a different egress rule. |

Record the results once per environment and store with your Helm value
overrides; revisit on cluster upgrades.

### HTTPS and secure authentication cookies

Do not set `backend.config.authCookieSecure=true` until HTTPS works end to
end. The safe manual rollout is:

1. Provision DNS, TLS termination, and the certificate.
2. Verify HTTPS while cookies are still non-secure.
3. Set `authAllowedOrigins` and `appBaseUrl` to the HTTPS frontend origin.
4. Set `authCookieSecure=true` and verify login/logout/session refresh.
5. Add HSTS in a later deployment after HTTPS has proven stable.

Rolling back from HTTPS to HTTP can leave browsers holding a Secure cookie
that they will not send over HTTP; affected users must return to HTTPS or
clear that cookie.

### NetworkPolicy staging

`networkPolicy.enabled` is deliberately false by default. Before enabling it,
record the environment's ingress-controller labels, internal load-balancer
source ranges, DNS labels, and Kubernetes API endpoint CIDRs. Apply and verify
explicit allow rules before default-deny enforcement.

The chart exposes a staged rollout via `networkPolicy.mode`. Advance one
step at a time, validating connectivity between steps:

1. **`mode: allow-only`** — explicit allow rules render but no default-deny is
   applied. Use this to confirm rule selectors and CIDRs match real traffic
   without risk of blocking anything.
2. **`mode: enforce-ingress`** — adds default-deny for ingress only. Catches
   inbound surprises while egress remains open.
3. **`mode: enforce-all`** — adds default-deny for egress. Fully locked down;
   only the explicit egress rules apply (Qdrant, DNS, Kubernetes API,
   configured external HTTPS).

Stop the rollout at the highest mode that passes connectivity validation in
your environment. Downgrade by editing the value and re-applying — the
default-deny policy is removed cleanly on `helm upgrade`.

Stock Kubernetes NetworkPolicy is L3/L4 only and cannot restrict Gemini by
hostname. The supplied policy allows public TCP/443 while excluding private,
link-local, and metadata ranges. For hostname filtering, route egress through
an Envoy/HAProxy allow-list proxy or use a Cilium/Calico FQDN policy.

### Agent invoke API rate limiting

`AGENT_API_REQUESTS_PER_MINUTE` (default 30) is enforced in-process per token
fingerprint. The limiter is **not shared across Gunicorn/Uvicorn workers**, so
the effective ceiling is `AGENT_API_REQUESTS_PER_MINUTE × replicas × workers`.
Size the chart value accordingly, and treat the limit as a per-pod first line
of defense rather than a global cap. Back with Redis or another shared store
if a strict global cap is required.

`AGENT_API_MAX_CONCURRENCY` is similarly per-process. Total in-flight ReAct
executions across the deployment are `concurrency × replicas × workers`.
