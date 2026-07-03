# KubeAstra CLI

[![PyPI version](https://img.shields.io/pypi/v/kubeastra.svg)](https://pypi.org/project/kubeastra/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](../LICENSE)

AI-powered Kubernetes troubleshooting from your terminal. A thin HTTP + SSE client for the KubeAstra backend.

```
$ kubeastra investigate --pod api-gateway --ns production

$ kubeastra investigate
  thinking · The user wants a scoped investigation of api-gateway in production …
 kubectl  get_pods returned 1 result (48ms)
 kubectl  describe_pod api-gateway (162ms)
 logs     get_pod_logs — last 200 lines (89ms)
 events   correlated 3 recent events (54ms)
 analyze  investigate_pod complete (912ms)

─ answer ─
Root cause: OOMKilled. Container RSS 142Mi exceeded limit 128Mi.
Trigger: Memory leak introduced in v2.3.1 (deployed 47 min ago).

╭─ KubeAstra ─────────────────────────────────────────────────────────╮
│                                                                     │
│  Root cause                                                         │
│                                                                     │
│  The api-gateway container was OOMKilled 3 times in the last 30     │
│  minutes. Its resident memory (142Mi) exceeded the configured limit │
│  (128Mi) shortly after each restart. The pattern started at         │
│  14:23 UTC, coinciding with the v2.3.1 rollout at 14:22 UTC.        │
│                                                                     │
│  Recommended: Rollback to v2.3.0 · blast radius: 2 services         │
│                                                                     │
╰─ tool: investigate_pod · cost: $0.0021 · tokens: 640 ─────────────────╯
```

## Install

**pip / pipx (recommended for standalone use):**
```bash
pipx install kubeastra
```

**From the monorepo (for contributors):**
```bash
git clone https://github.com/astraverse-io/KubeAstra.git
cd KubeAstra/cli
pip install -e .
```

Python 3.11 or newer is required.

## Prerequisites

The CLI talks to a KubeAstra backend over HTTP + Server-Sent Events. Start one before running commands:

```bash
# Local backend via docker-compose (from the monorepo root):
cd ui && docker compose up -d --build
# Backend listens on http://localhost:8000 by default.
```

Or point the CLI at an existing Helm-deployed backend:

```bash
kubeastra config set backend-url https://kubeastra.mycompany.internal
```

## Commands

### `kubeastra ask "..."`
Natural-language investigation. Streams the ReAct loop step-by-step.

```bash
kubeastra ask "why is checkout-service crashlooping in production?"
kubeastra ask "what pods are in the payments namespace?"
```

### `kubeastra investigate`
Scoped investigation shortcut — same underlying flow as `ask`, with a canned question.

```bash
kubeastra investigate --pod api-gateway --ns production
kubeastra investigate --deployment redis --ns default
kubeastra investigate --node k8s-worker-01
```

### `kubeastra connect`
Pick a kubeconfig context. Without arguments, lists what's available.

```bash
kubeastra connect                            # list contexts
kubeastra connect --context prod-us-east-1   # connect to a specific context
kubeastra connect --context dev --kubeconfig ~/.kube/other-config
```

### `kubeastra config set / get`
Persist backend URL, API token (if the backend enforces auth), and session id.

```bash
kubeastra config set backend-url https://kubeastra.example.com
kubeastra config set api-token ka_prod_abcdef1234567890
kubeastra config get                         # print all values (token masked)
kubeastra config get backend-url             # print a specific value
kubeastra config path                        # print the config file path
```

Config lives at `~/.config/kubeastra/config.toml` on Linux/macOS
(`$XDG_CONFIG_HOME/kubeastra/config.toml` when set) and
`%APPDATA%\kubeastra\config.toml` on Windows.

### `kubeastra doctor`
Health-check the CLI + backend + kubeconfig detection.

```bash
kubeastra doctor
```

### `kubeastra --version`
Print the CLI version.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Generic CLI error (bad flags, unexpected exception) |
| `2` | Authentication failure — set an API token |
| `3` | Connection failure — backend unreachable |
| `4` | Backend returned a non-2xx status |
| `5` | Backend answered but the investigation itself errored |

## Design notes

- **The CLI is a client, not an engine.** The ReAct loop, tool dispatch, and RAG live in the backend. This is by design — one implementation of the agent, three surfaces (web UI, MCP server, this CLI).
- **No offline mode.** Air-gapped deployments should run the backend Helm chart on-cluster and point the CLI at the internal service.
- **No secrets in the config file** beyond what you put there. The `api_token` field is intentionally the only persisted credential.
- **No telemetry.** The CLI does not phone home. Only the backend URL you configure is contacted.

## Related

- [KubeAstra README](../README.md) — the full project
- [Web UI](../ui/README.md) — the same investigations, in a browser
- [MCP server](../mcp/README.md) — the same tools, in your IDE (Cursor / Claude Desktop)
- [Marketing site](https://kubeastra.io)

## License

Apache 2.0 — see [LICENSE](../LICENSE).
