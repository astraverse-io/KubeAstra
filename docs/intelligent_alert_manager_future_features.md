# Future Features: Intelligent Alert Manager

This document outlines the deepest, most logical next steps to extend the Intelligent Alert Manager into a true "one-stop shop" for Kubernetes operations, investigations, and remediation.

## 1. Human-in-the-Loop Remediation (Write Operations)
Right now, the agent is an incredible *reader* and *investigator*. The natural next step is allowing it to be a *fixer*.
*   **The Feature:** When an investigation completes, the agent doesn't just provide an RCA; it proposes a concrete remediation plan (e.g., "Scale deployment X to 3 replicas," "Rollback to previous revision," or "Restart the crashing pod"). 
*   **How it works:** We add write-based MCP tools (`kubectl_scale`, `kubectl_rollout_undo`) but wrap them in an approval layer. The agent surfaces a proposed command in the Next.js chat UI with a big **"Approve & Run"** button. The human stays in the loop, but the MTTR (Mean Time to Resolution) drops to seconds.

## 2. "GitOps & CI/CD" Context Integration
When things break in Kubernetes, 90% of the time it's because someone changed something.
*   **The Feature:** Give the agent access to your deployment systems (ArgoCD, Flux, GitHub Actions, GitLab CI).
*   **How it works:** When an alert fires (e.g., `High5xxErrors`), the playbook automatically checks for deployments made in the last 60 minutes. The RCA changes from *"The pod is crashing"* to *"The pod is crashing because Commit #a1b2c3d bumped the database dependency, and the readiness probe is failing. Should I revert the ArgoCD app?"*

## 3. Incident Memory & Vector Similarity (The "Déjà Vu" Engine)
You have Qdrant referenced in your `settings.py`. It's time to leverage it heavily.
*   **The Feature:** The system learns from past outages.
*   **How it works:** Every time an RCA is completed and human-verified, its symptoms and solution are embedded into the Qdrant vector database. When a new alert arrives, the first step of the orchestrator is a semantic search: *"Have we seen this exact failure pattern before?"* The agent can instantly say, *"This is the same Redis connection pool exhaustion we saw last Tuesday. The runbook is to increase the max connections in the configmap."*

## 4. Proactive Cluster Health Scanning
Why wait for an alert to fire?
*   **The Feature:** A scheduled cron job that runs a "Morning Health Check" on the cluster.
*   **How it works:** The agent autonomously spins up every morning, runs tools like `k8sgpt`, checks for orphaned resources, pods without resource limits, expiring TLS certificates, or nodes with high disk pressure. It drops a daily briefing in the chat UI: *"Cluster is healthy, but 3 deployments are lacking memory limits and cert-manager has a certificate expiring in 4 days."*

## 5. Multi-Cluster Fleet Management
If you manage Kubernetes, you likely manage *more than one* cluster (Dev, Staging, Prod, across multiple regions).
*   **The Feature:** Native multi-cluster context switching.
*   **How it works:** Ensure every webhook payload explicitly passes a `cluster_id`. The MCP tools and `kubectl_runner.py` dynamically switch `KUBECONFIG` contexts before executing. You can then use the chat UI to ask comparative questions: *"Why is the payment service working in Staging but crash-looping in Prod? Compare their configmaps."*

## 6. Dynamic PromQL & LogQL Query Generation
Currently, the agent relies heavily on Kubernetes API state (events, pod statuses). 
*   **The Feature:** Let the agent dive deep into the raw telemetry.
*   **How it works:** Instead of hardcoded metrics checks, give the agent tools like `execute_promql` and `execute_logql`. If a pod says `OOMKilled`, the LLM dynamically writes a PromQL query to plot the memory usage of that specific pod over the last 30 minutes, fetching the time-series data and summarizing it to prove *when* the spike happened.
