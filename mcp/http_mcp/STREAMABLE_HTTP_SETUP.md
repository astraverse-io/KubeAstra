# MCP Streamable HTTP - Quick Setup Guide

## What Changed

We've upgraded the HTTP MCP server to use the **Streamable HTTP protocol** (March 2025+ MCP specification), which is the official, modern way to run MCP servers over HTTP.

## Files in This Module

1. **`http_server.py`** - MCP Streamable HTTP server using `StreamableHTTPSessionManager` (session-aware, auth, debug endpoints)
2. **`http_client.py`** - Example Python client for the HTTP transport
3. **`README.md`** - Full documentation with all options and examples

## Quick Start

### 1. Install Dependencies

```bash
cd mcp
pip install "mcp[streamable-http]>=1.0.0"
```

### 2. Start Server

```bash
python http_mcp/http_server.py --port 8001
```

You should see:
```
INFO  Starting KubeAstra MCP HTTP server
INFO  Health:      http://127.0.0.1:8001/health
INFO  Debug tools: http://127.0.0.1:8001/debug/tools
INFO  MCP (HTTP):  http://127.0.0.1:8001/mcp/
INFO  HTTP MCP server ready — 127.0.0.1:8001/mcp/ — 48 tools registered
```

### 3. Verify

```bash
curl http://127.0.0.1:8001/health
```

Expected response:
```json
{
  "status": "ok",
  "server": "mcp",
  "transport": "Streamable HTTP",
  "protocol": "MCP March 2025+"
}
```

### 4. Configure Cursor

Edit `~/.cursor/mcp.json`:

**Option A: HTTP only** (recommended for remote/shared server)
```json
{
  "mcpServers": {
    "kubeastra-http": {
      "type": "http",
      "url": "http://127.0.0.1:8001/mcp/"
    }
  }
}
```

**Option B: Keep both stdio and HTTP** (Cursor will use both)
```json
{
  "mcpServers": {
    "kubeastra": {
      "command": "/path/to/venv/bin/python",
      "args": ["/path/to/mcp_server/server.py"]
    },
    "kubeastra-http": {
      "type": "http",
      "url": "http://127.0.0.1:8001/mcp/"
    }
  }
}
```

**Important:** If you configure both, Cursor will connect to both servers and discover tools from each. When you ask a natural language question like "show me pods in default namespace", Cursor's AI automatically routes it to the appropriate server. You don't need to specify which one to use.

Restart Cursor and you're done! All 48 tools will be available.

## Test Queries

### From curl

```bash
# Get pods in default namespace
curl -X POST http://127.0.0.1:8001/mcp// \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "get_pods",
      "arguments": {"namespace": "default"}
    }
  }' | jq

# AI-powered pod investigation
curl -X POST http://127.0.0.1:8001/mcp// \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
      "name": "investigate_pod",
      "arguments": {
        "namespace": "prod",
        "pod_name": "payment-service-7d4f9b"
      }
    }
  }' | jq
```

### From Python

```python
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def test():
    async with streamable_http_client("http://127.0.0.1:8001/mcp/") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            
            # Call a tool
            result = await session.call_tool(
                "get_pods",
                arguments={"namespace": "default"}
            )
            print(result.content[0].text)

asyncio.run(test())
```

### From JavaScript

```javascript
async function callTool(toolName, args) {
  const response = await fetch('http://127.0.0.1:8001/mcp//', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'tools/call',
      params: {name: toolName, arguments: args}
    })
  });
  return await response.json();
}

// Get pods
const result = await callTool('get_pods', {namespace: 'default'});
console.log(result);
```

## Why Streamable HTTP?

### Before (Old Approach)
- ❌ Custom REST API (non-standard)
- ❌ Manual JSON-RPC handling
- ❌ Complex SSE/WebSocket setup
- ❌ Not officially supported by Cursor

### After (Streamable HTTP)
- ✅ Official MCP protocol
- ✅ Native Cursor support
- ✅ Bidirectional streaming over HTTP
- ✅ Simple, stateless, scalable
- ✅ Works with any HTTP client

## Next Steps

1. **Test locally** - Start the server and try the curl examples
2. **Configure Cursor** - Add to `~/.cursor/mcp.json` and restart
3. **Deploy to K8s** (optional) - See README.md for Kubernetes deployment
4. **Delete old file** (optional) - Remove `*(deleted — superseded by current http_server.py)*` if not needed

## Documentation

See `README.md` for:
- Complete architecture explanation
- All usage examples (Python, JavaScript, curl, shell scripts)
- Kubernetes deployment guide
- Troubleshooting tips
- Performance tuning

## Support

If you encounter issues:
1. Check server logs: `python http_mcp/http_server.py`
2. Verify health: `curl http://127.0.0.1:8001/health`
3. Check Cursor config: `~/.cursor/mcp.json`
4. Ensure MCP SDK installed: `pip show mcp`
