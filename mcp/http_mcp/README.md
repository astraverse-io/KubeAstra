# HTTP MCP Transport

Expose the shared `mcp` toolset over the **Streamable HTTP protocol** (MCP March 2025+ specification) — the official, modern way to run MCP servers over HTTP that Cursor v1.0+ expects.

This transport is intended for:
- Connecting from another IDE or machine without a local stdio process
- Shared/central team deployments where multiple users connect to one server
- Testing a remote-style MCP connection locally before deploying
- CI/CD or script-based access to the MCP tools

---

## What Gets Exposed

The HTTP server uses **the exact same shared registrations** as the stdio server:

- Same 48 tool schemas (33 live kubectl + 5 helm + 6 AI analysis + 3 multi-step plan + 1 knowledge base)
- Same kubectl + SSH runner logic
- Same Gemini / Weaviate integrations
- Same namespace and recovery-operation safeguards
- Same dry-run + confirmation-token safety on destructive ops (see [mcp/README.md](../README.md))
- Same `runtime.py` bootstrap (tool count is auto-discovered at startup)

The `/tools/catalog` page groups tools into categories: Discovery, Details, Context, Repository, Analysis, **Helm** (read-only release inspection), Recovery (WRITE), **Plans (WRITE)** — for the multi-step remediation tools — and AI Analysis.

There is no second tool implementation for HTTP mode.

---

## Why Streamable HTTP vs. the old SSE/WebSocket implementation?

| Feature | Old custom SSE/WS | Streamable HTTP (current) |
|---|---|---|
| MCP spec compliance | ❌ Custom protocol | ✅ Official March 2025 spec |
| Cursor v1.0+ support | ❌ Workaround needed | ✅ Native `"type": "http"` config |
| Session management | ❌ Stateless | ✅ `StreamableHTTPSessionManager` |
| Auth | ❌ None | ✅ Bearer token |
| Debug endpoints | ❌ None | ✅ `/debug/tools`, `/debug/call` |

---

## Quick Start

```bash
cd k8s-devops-ai-assistant/mcp
./setup.sh          # first time only
make run-http       # starts on 127.0.0.1:8001/mcp/
```

Default local endpoints:

| Endpoint | Description |
|---|---|
| `http://127.0.0.1:8001/mcp/` | MCP Streamable HTTP transport |
| `http://127.0.0.1:8001/health` | Health check |
| `http://127.0.0.1:8001/debug/tools` | List all registered tools |
| `http://127.0.0.1:8001/debug/call` | Call a tool directly |
| `http://127.0.0.1:8001/docs` | Swagger UI |

For network-accessible (team/shared) deployment:

```bash
make run-http-external   # binds to 0.0.0.0:8001
```

---

## Cursor / IDE Config

Edit `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "k8s-devops-http": {
      "type": "http",
      "url": "http://127.0.0.1:8001/mcp/"
    }
  }
}
```

> **Note:** Use `"type": "http"` — Cursor v1.0+ uses this to identify the Streamable HTTP protocol automatically.

You can run both stdio and HTTP at the same time (Cursor will deduplicate tools automatically):

```json
{
  "mcpServers": {
    "k8s-devops": {
      "command": "/path/to/venv/bin/python",
      "args": ["/path/to/mcp_server/server.py"]
    },
    "k8s-devops-http": {
      "type": "http",
      "url": "http://127.0.0.1:8001/mcp/"
    }
  }
}
```

### With Auth Token

```bash
export MCP_HTTP_AUTH_TOKEN=my-team-token
make run-http
```

```json
{
  "mcpServers": {
    "k8s-devops-http": {
      "type": "http",
      "url": "http://127.0.0.1:8001/mcp/",
      "headers": {
        "Authorization": "Bearer my-team-token"
      }
    }
  }
}
```

---

## Local Smoke Tests

```bash
# Health check
curl http://127.0.0.1:8001/health

# List all registered tools
curl http://127.0.0.1:8001/debug/tools | jq '.tools_count, [.tools[].name]'

# Call a tool directly
curl -X POST http://127.0.0.1:8001/debug/call \
  -H "Content-Type: application/json" \
  -d '{"tool_name": "get_current_context", "arguments": {}}'

# Or use the example Python client against the real MCP transport
PYTHONPATH=. venv/bin/python http_mcp/http_client.py \
  --url http://127.0.0.1:8001/mcp/
```

---

## Runtime Options

```bash
PYTHONPATH=. venv/bin/python http_mcp/http_server.py --help
```

| Flag | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address (`0.0.0.0` for network) |
| `--port` | `8001` | Port |
| `--mcp-path` | `/mcp` | HTTP path for MCP transport |
| `--auth-token` | *(none)* | Bearer token — clients must send `Authorization: Bearer <token>` |
| `--json-response` | off | Return JSON bodies instead of SSE streams |
| `--stateless` | off | Disable server-side session tracking |
| `--reload` | off | Auto-reload on code changes (dev only) |

Environment variable equivalents: `MCP_HTTP_HOST`, `MCP_HTTP_PORT`, `MCP_HTTP_PATH`, `MCP_HTTP_AUTH_TOKEN`.

---

## Files in This Directory

| File | Purpose |
|---|---|
| `http_server.py` | MCP Streamable HTTP server (March 2025 protocol) |
| `http_client.py` | Example Python client for the HTTP transport |
| `README.md` | This file |
| `CURSOR_ROUTING.md` | How Cursor routes queries across stdio and HTTP MCP servers |
| `STREAMABLE_HTTP_SETUP.md` | Quick setup reference |
| `INVOKE_FROM_PROJECTS.md` | How to use from other repositories |
| `__init__.py` | Package marker |

---

## Notes

- `make run-http` binds to `127.0.0.1` by default — safe for local testing.
- `make run-http-external` binds to `0.0.0.0` — for team/shared deployments, add TLS and a reverse proxy (nginx/Traefik) in front.
- The HTTP transport runs independently from the `ui` web backend (port 8000). They don't conflict.
- Session state is maintained by `StreamableHTTPSessionManager` — each client gets a stable session across multiple requests.
