# Using The HTTP MCP Server From Another Project

This repo now exposes the shared KubeAstra MCP toolset over a real Streamable HTTP endpoint.

## Start The Server

```bash
cd /Users/pruthvidavineni/AI_DevOps_Assistant/kubeastra-ai-assistant/mcp
make run-http
```

Default MCP URL:

```text
http://127.0.0.1:8001/mcp/
```

## Option 1: Configure Another IDE

Example remote MCP config:

```json
{
  "mcpServers": {
    "kubeastra-http": {
      "url": "http://127.0.0.1:8001/mcp/"
    }
  }
}
```

With auth:

```json
{
  "mcpServers": {
    "kubeastra-http": {
      "url": "http://127.0.0.1:8001/mcp/",
      "headers": {
        "Authorization": "Bearer dev-local-token"
      }
    }
  }
}
```

## Option 2: Use The Example Python Client

```bash
cd /Users/pruthvidavineni/AI_DevOps_Assistant/kubeastra-ai-assistant/mcp
PYTHONPATH=. venv/bin/python http_mcp/http_client.py \
  --url http://127.0.0.1:8001/mcp/
```

Call a specific tool:

```bash
PYTHONPATH=. venv/bin/python http_mcp/http_client.py \
  --url http://127.0.0.1:8001/mcp/ \
  --tool get_current_context \
  --args-json '{}'
```

## Option 3: Manual Local Smoke Test

Health:

```bash
curl http://127.0.0.1:8001/health
```

Debug tool list:

```bash
curl http://127.0.0.1:8001/debug/tools
```

Debug tool call:

```bash
curl -X POST http://127.0.0.1:8001/debug/call \
  -H "Content-Type: application/json" \
  -d '{
    "tool_name": "get_current_context",
    "arguments": {}
  }'
```

## Architecture

```text
Another IDE / Remote Client
    -> Streamable HTTP MCP endpoint (/mcp)
    -> Shared MCP Core registrations
    -> Kubernetes / SSH / Gemini / Weaviate
```

The web app backend is not part of this path.
