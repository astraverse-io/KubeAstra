# Demo Mode

Spin up a local Kubernetes cluster pre-seeded with realistic broken workloads so you can see the assistant work in 60 seconds — no production cluster needed.

---

## What you get

- A two-node [kind](https://kind.sigs.k8s.io/) cluster (`kubeastra-demo`)
- A `demo` namespace containing six intentionally-broken workloads covering the most common Kubernetes failure modes
- The web UI running locally at http://localhost:3300, already pointed at the demo cluster

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/)
- [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- An LLM: either a [Google Gemini API key](https://aistudio.google.com/) (free tier works) **or** [Ollama](https://ollama.com/) running locally

---

## Quick Start

From the repository root:

```bash
make demo
```

The demo uses `ui/backend/.env` for backend settings such as `GEMINI_API_KEY`, `LLM_PROVIDER`, and `ALLOWED_NAMESPACES`.
It also auto-generates a demo-only kubeconfig and a temporary Compose env override for the Dockerized backend, so the web UI targets the `kind-kubeastra-demo` cluster even if your host machine is pointed at a different kubectl context, without modifying `ui/.env`.

Or equivalently:

```bash
cd demo
make up
```

This will:

1. Create the kind cluster (reused if it already exists)
2. Apply the six broken workloads into the `demo` namespace
3. Wait for the broken states to manifest
4. Start the web UI (backend + frontend) via docker-compose

Open http://localhost:3300 and try:

- *"what's broken in the demo namespace?"*
- *"why is payment-service crashing?"*
- *"why is the redis-master pod pending?"*
- *"generate a runbook for the stuck PVC issue"*

---

## The Broken Workloads

| File | Scenario | What the assistant should diagnose |
|---|---|---|
| [`01-crashloop.yaml`](broken-workloads/01-crashloop.yaml) | `CrashLoopBackOff` | `payment-service` exits 1 with `connection refused to redis-master:6379` |
| [`02-oom.yaml`](broken-workloads/02-oom.yaml) | `OOMKilled` | `memory-hog` allocates 128Mi but its limit is 48Mi |
| [`03-imagepull.yaml`](broken-workloads/03-imagepull.yaml) | `ErrImagePull` / `ImagePullBackOff` | Image tag `v9.9.9-does-not-exist` can't be pulled |
| [`04-pending-nodeselector.yaml`](broken-workloads/04-pending-nodeselector.yaml) | `Pending` | `nodeSelector: accelerator=nvidia-tesla-v100` matches no node |
| [`05-stuck-pvc.yaml`](broken-workloads/05-stuck-pvc.yaml) | `Pending` (unbound PVC) | PVC requests a storage class that doesn't exist; dependent `redis-master` pod can't schedule |
| [`06-rbac-denied.yaml`](broken-workloads/06-rbac-denied.yaml) | `Forbidden` in logs | `ops-reporter` runs as a ServiceAccount with no cluster read permissions |
| [`07-secret-missing-job.yaml`](broken-workloads/07-secret-missing-job.yaml) | `CreateContainerConfigError` | Job `backup-reporting-job` mounts `DATABASE_PRIVATE_KEY` from a secret that does not exist |

These seven cover the overwhelming majority of real-world incidents a DevOps engineer gets paged for.

---

## Useful commands

```bash
make status     # kubectl get pods,pvc -n demo -o wide
make logs       # tail the web UI backend logs
make down       # remove workloads + stop UI (keep cluster)
make clean      # delete everything, including the kind cluster
```

---

## Manual control

If you prefer to drive things yourself:

```bash
# Cluster
kind create cluster --name kubeastra-demo --config demo/kind-config.yaml

# Workloads
kubectl apply -f demo/broken-workloads/

# Watch them break
kubectl get pods -n demo -w

# Web UI (separate terminal)
cd ui
docker compose up
```

---

## Adding your own scenarios

Drop a new YAML into `broken-workloads/`. A few tips:

- Put all resources in the `demo` namespace so `make down` cleans them up
- Keep resource requests tiny (kind nodes are small)
- Give each resource a `scenario: <name>` label so they're easy to find
- Document the expected symptom in this README's table so the diagnosis is verifiable
