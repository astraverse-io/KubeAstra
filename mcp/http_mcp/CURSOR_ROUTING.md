# How Cursor Routes Natural Language Queries to MCP Servers

## The Question You Asked

> "If I just say 'I'm seeing this error: ImagePullBackOff', how does Cursor know whether to use stdio or HTTP MCP?"

**Answer: Cursor doesn't differentiate - it treats them the same!**

## How It Actually Works

### Step 1: Cursor Connects to ALL Configured Servers

When Cursor starts, it reads `~/.cursor/mcp.json` and connects to every server listed:

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

**What Cursor does:**
```
1. Starts stdio process for "k8s-devops"
2. Connects to HTTP server for "k8s-devops-http"
3. Calls tools/list on BOTH servers
4. Builds a unified tool registry
```

### Step 2: Cursor Discovers Tools from Each Server

```
k8s-devops (stdio):
  ✓ get_pods
  ✓ describe_pod
  ✓ investigate_pod
  ✓ analyze_error
  ... (48 tools total)

k8s-devops-http (HTTP):
  ✓ get_pods
  ✓ describe_pod
  ✓ investigate_pod
  ✓ analyze_error
  ... (48 tools total)
```

**Result:** Cursor now has 48 tools available (or 96 if both servers expose the same tools).

### Step 3: You Ask a Natural Language Question

```
User: "I'm seeing this error: ImagePullBackOff: Failed to pull image"
```

### Step 4: Cursor's AI Decides Which Tool to Call

```
Cursor AI analyzes the question:
  - Detects: Error message about Kubernetes
  - Matches: "analyze_error" tool
  - Checks: Which server(s) have "analyze_error"?
  
Available options:
  1. k8s-devops (stdio) → analyze_error
  2. k8s-devops-http (HTTP) → analyze_error
  
Cursor picks ONE (typically the first available or most reliable)
```

### Step 5: Cursor Routes the Request

```
Cursor calls: analyze_error(
  error_text="ImagePullBackOff: Failed to pull image",
  tool="kubernetes",
  environment="unknown"
)

Via: k8s-devops-http (HTTP)
  ↓
POST http://127.0.0.1:8001/mcp//
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "analyze_error",
    "arguments": {...}
  }
}
```

### Step 6: Response Flows Back to Cursor

```
HTTP Server → Cursor → User

User sees: AI analysis of the ImagePullBackOff error
```

---

## Key Insights

### 1. You DON'T Specify the Transport in Natural Language

❌ **You don't say:**
```
"Use the HTTP server to analyze this error: ImagePullBackOff"
```

✅ **You just say:**
```
"I'm seeing this error: ImagePullBackOff"
```

Cursor figures out the rest!

### 2. Cursor Treats stdio and HTTP Identically

From Cursor's perspective:
- **stdio server** = MCP server (happens to use stdin/stdout)
- **HTTP server** = MCP server (happens to use HTTP)

Both expose the same MCP protocol, so Cursor doesn't care about the transport.

### 3. If Both Servers Have the Same Tool

**Scenario:** Both `k8s-devops` (stdio) and `k8s-devops-http` (HTTP) expose `get_pods`.

**What happens:**
- Cursor sees TWO `get_pods` tools
- Cursor picks one (typically the first in config, or based on availability)
- You can't control which one Cursor uses via natural language

**Solution if you want to avoid duplicates:**
- Only configure ONE server (either stdio OR HTTP, not both)
- OR: Give them different names/namespaces if you want both

### 4. When You WOULD Specify the Server

You can't specify the server in natural language, but you CAN in direct tool calls:

```python
# Python MCP SDK - you explicitly choose the server
async with streamable_http_client("http://127.0.0.1:8001/mcp/") as (read, write):
    # This ONLY uses the HTTP server
    result = await session.call_tool("get_pods", {...})
```

```bash
# curl - you explicitly choose the server
curl http://127.0.0.1:8001/mcp//  # HTTP server
# vs
# (no way to curl stdio directly)
```

---

## Practical Recommendations

### Scenario 1: Local Development (You Only)

**Use stdio only:**
```json
{
  "mcpServers": {
    "k8s-devops": {
      "command": "/path/to/venv/bin/python",
      "args": ["/path/to/mcp_server/server.py"]
    }
  }
}
```

**Why:** Simpler, no HTTP server needed, easier to debug.

### Scenario 2: Remote Server or Team Sharing

**Use HTTP only:**
```json
{
  "mcpServers": {
    "k8s-devops-http": {
      "type": "http",
      "url": "http://your-server:8002"
    }
  }
}
```

**Why:** Multiple people can connect, server can run in Kubernetes, accessible from any machine.

### Scenario 3: Testing Both Transports

**Use both with different names:**
```json
{
  "mcpServers": {
    "k8s-devops-local": {
      "command": "/path/to/venv/bin/python",
      "args": ["/path/to/mcp_server/server.py"]
    },
    "k8s-devops-remote": {
      "type": "http",
      "url": "http://127.0.0.1:8001/mcp/"
    }
  }
}
```

**Why:** You can test both, but Cursor will see duplicate tools. Use different server names to distinguish them in logs.

---

## Visual Flow

```
┌─────────────────────────────────────────────────────────────┐
│  You: "I'm seeing ImagePullBackOff error"                  │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ↓
┌─────────────────────────────────────────────────────────────┐
│  Cursor AI                                                  │
│  - Analyzes question                                        │
│  - Matches to "analyze_error" tool                          │
│  - Checks which server(s) have it                           │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ↓
┌─────────────────────────────────────────────────────────────┐
│  Available Servers (from ~/.cursor/mcp.json)                │
│                                                             │
│  Option 1: k8s-devops (stdio)                               │
│    ├─ analyze_error ✓                                       │
│    └─ 47 other tools                                        │
│                                                             │
│  Option 2: k8s-devops-http (HTTP)                           │
│    ├─ analyze_error ✓                                       │
│    └─ 47 other tools                                        │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ↓
┌─────────────────────────────────────────────────────────────┐
│  Cursor picks ONE server (e.g., k8s-devops-http)            │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ↓
┌─────────────────────────────────────────────────────────────┐
│  HTTP Request                                               │
│  POST http://127.0.0.1:8001/mcp//                                │
│  {                                                          │
│    "method": "tools/call",                                  │
│    "params": {                                              │
│      "name": "analyze_error",                               │
│      "arguments": {"error_text": "ImagePullBackOff..."}     │
│    }                                                        │
│  }                                                          │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ↓
┌─────────────────────────────────────────────────────────────┐
│  MCP Server (http_server.py)                                │
│  - Receives request                                         │
│  - Calls analyze_error tool                                 │
│  - Returns AI analysis                                      │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ↓
┌─────────────────────────────────────────────────────────────┐
│  Response to Cursor                                         │
│  {                                                          │
│    "result": {                                              │
│      "content": [{                                          │
│        "text": "Root cause: ImagePullBackOff means..."      │
│      }]                                                     │
│    }                                                        │
│  }                                                          │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ↓
┌─────────────────────────────────────────────────────────────┐
│  You see: AI analysis of the error                          │
└─────────────────────────────────────────────────────────────┘
```

---

## Summary

**Q: How does Cursor differentiate between stdio and HTTP when I ask a natural language question?**

**A: It doesn't!** 

- Cursor connects to ALL configured servers (stdio, HTTP, or both)
- Cursor discovers tools from each server
- When you ask a question, Cursor's AI picks the appropriate tool
- Cursor automatically routes to whichever server has that tool
- You never specify the transport in natural language

**The transport (stdio vs HTTP) is transparent to you as the user!**
