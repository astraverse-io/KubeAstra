"""Run the K8s DevOps MCP server over Streamable HTTP.

This exposes the same shared MCP toolset over a spec-compliant Streamable HTTP
transport (MCP March 2025+ protocol) that Cursor v1.0+ and other MCP clients
can connect to directly.

Highlights over the old SSE/WebSocket implementation:
  - Uses the official MCP SDK StreamableHTTPSessionManager for proper session tracking
  - Optional bearer-token auth for team/shared deployments
  - /debug/tools and /debug/call endpoints for local smoke tests
  - Configurable MCP path, JSON-response mode, and stateless mode
  - Shares tool registrations, schemas, and runtime.py bootstrap with the stdio server
    (no second tool implementation)
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import time
from argparse import ArgumentParser
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
from starlette.responses import Response

# Add MCP root to path
_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

try:
    from mcp.server import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    import mcp.types as types
    from mcp_server.runtime import build_server, list_registered_tools, log_runtime_settings
except ImportError as e:
    print(f"Error importing MCP modules: {e}")
    print("Make sure to run: pip install -r requirements.txt")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _agent_debug_log(location: str, message: str, data: dict[str, Any], hypothesis_id: str) -> None:
    payload = {
        "sessionId": "1505ce",
        "runId": "initial",
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    try:
        with open("/Users/pruthvidavineni/AI_DevOps_Assistant/.cursor/debug-1505ce.log", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
    except Exception:
        pass


# ── Configuration ─────────────────────────────────────────────────────────────

class HTTPConfig(BaseModel):
    """Runtime settings for the HTTP MCP server.

    All fields can be set via environment variables so the server can be
    configured without rebuilding the container image.
    """

    host: str = Field(default_factory=lambda: os.getenv("MCP_HTTP_HOST", "127.0.0.1"))
    port: int = Field(default_factory=lambda: int(os.getenv("MCP_HTTP_PORT", "8001")))
    mcp_path: str = Field(default_factory=lambda: os.getenv("MCP_HTTP_PATH", "/mcp"))
    auth_token: str | None = Field(default_factory=lambda: os.getenv("MCP_HTTP_AUTH_TOKEN") or None)
    json_response: bool = False   # set True to get JSON bodies instead of SSE streams
    stateless: bool = False       # set True to disable server-side session tracking


class DebugToolCallRequest(BaseModel):
    """Request body for the /debug/call smoke-test endpoint."""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_mcp_path(path: str) -> str:
    """Ensure the MCP path starts with / and ends with / for mount compatibility."""
    if not path.startswith("/"):
        path = f"/{path}"
    path = path.rstrip("/")
    if not path:
        path = "/mcp"
    return f"{path}/"


def _authorize_request(request: Request, auth_token: str | None) -> None:
    """Raise HTTP 401 if a bearer token is configured and the request doesn't supply it."""
    if not auth_token:
        # region agent log
        _agent_debug_log(
            "http_mcp/http_server.py:_authorize_request",
            "MCP auth not configured for request",
            {
                "method": request.method,
                "path": request.url.path,
                "authEnabled": False,
                "hasAuthorization": bool(request.headers.get("authorization")),
            },
            "H2",
        )
        # endregion
        return
    auth_header = request.headers.get("authorization", "")
    expected = f"Bearer {auth_token}"
    # region agent log
    _agent_debug_log(
        "http_mcp/http_server.py:_authorize_request",
        "MCP auth evaluated for request",
        {
            "method": request.method,
            "path": request.url.path,
            "authEnabled": True,
            "hasAuthorization": bool(auth_header),
            "authScheme": auth_header.split(" ", 1)[0] if auth_header else None,
            "matchesExpected": secrets.compare_digest(auth_header, expected),
        },
        "H2",
    )
    # endregion
    if not secrets.compare_digest(auth_header, expected):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")


def _categorize_tools(tools: list[Any]) -> dict[str, list[Any]]:
    """Categorize tools based on their names and descriptions."""
    categories = {
        "kubectl_discovery": [],
        "kubectl_details": [],
        "kubectl_context": [],
        "kubectl_repo": [],
        "kubectl_analysis": [],
        "kubectl_recovery": [],
        "kubectl_plans": [],
        "helm": [],
        "ai_analysis": [],
    }

    for tool in tools:
        name = tool.name
        desc = tool.description.lower() if tool.description else ""

        if name in ["analyze_error", "get_fix_commands", "list_error_categories",
                    "cluster_report", "error_summary", "generate_runbook"]:
            categories["ai_analysis"].append(tool)
        elif name in ["propose_remediation_plan", "get_plan", "execute_plan_step"]:
            categories["kubectl_plans"].append(tool)
        elif name in ["delete_pod", "exec_pod_command", "rollout_restart",
                      "scale_deployment", "apply_patch"]:
            categories["kubectl_recovery"].append(tool)
        elif name in ["add_kubeconfig_context", "list_kubeconfig_contexts", 
                      "switch_kubeconfig_context", "get_current_context"]:
            categories["kubectl_context"].append(tool)
        elif name in ["search_deployment_repo", "get_deployment_repo_file",
                      "list_deployment_repo_path"]:
            categories["kubectl_repo"].append(tool)
        elif name in ["helm_available", "list_helm_releases", "get_helm_release",
                      "diff_helm_revisions", "investigate_helm_release"]:
            categories["helm"].append(tool)
        elif name in ["find_workload", "get_pods", "get_namespaces",
                      "list_namespace_resources", "list_services", "k8sgpt_analyze",
                      "search_configmaps", "get_configmap"]:
            categories["kubectl_discovery"].append(tool)
        elif name in ["describe_pod", "get_pod_logs", "get_events", "get_deployment", 
                      "get_service", "get_endpoints", "get_rollout_status", "get_resource_graph",
                      "investigate_workload", "analyze_namespace", "investigate_pod"]:
            categories["kubectl_details"].append(tool)
        elif "analysis" in desc or "investigate" in name:
            categories["kubectl_analysis"].append(tool)
        else:
            categories["kubectl_discovery"].append(tool)

    return {k: v for k, v in categories.items() if v}


def _render_catalog_html(categories: dict[str, list[Any]]) -> str:
    """Render tool catalog as HTML."""
    category_titles = {
        "kubectl_discovery": "🔍 Discovery — Find workloads & resources",
        "kubectl_details": "📋 Details — Get deep resource info",
        "kubectl_context": "🔄 Context — Manage kubeconfig",
        "kubectl_repo": "📦 Repository — Search deployment configs",
        "kubectl_analysis": "📊 Analysis — Complex investigations",
        "kubectl_recovery": "🔧 Recovery — Remediation ops (WRITE)",
        "kubectl_plans": "🗂️ Plans — Multi-step remediation (WRITE)",
        "ai_analysis": "🤖 AI Analysis — provider-backed diagnostics",
    }

    html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>K8s DevOps Tool Catalog</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 2rem 1rem;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }
        header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 3rem 2rem;
            text-align: center;
        }
        header h1 {
            font-size: 2.5rem;
            margin-bottom: 0.5rem;
        }
        header p {
            font-size: 1.1rem;
            opacity: 0.95;
        }
        .stats {
            display: flex;
            justify-content: center;
            gap: 2rem;
            margin-top: 1.5rem;
            flex-wrap: wrap;
        }
        .stat {
            text-align: center;
        }
        .stat-number {
            font-size: 2rem;
            font-weight: bold;
        }
        .stat-label {
            font-size: 0.9rem;
            opacity: 0.9;
        }
        .content {
            padding: 3rem 2rem;
        }
        .category {
            margin-bottom: 3rem;
        }
        .category-title {
            font-size: 1.5rem;
            font-weight: 600;
            color: #333;
            margin-bottom: 1.5rem;
            padding-bottom: 0.5rem;
            border-bottom: 3px solid #667eea;
        }
        .tools-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(350px, 1fr));
            gap: 1.5rem;
        }
        .tool-card {
            border: 1px solid #e0e0e0;
            border-radius: 8px;
            padding: 1.5rem;
            background: #f9fafb;
            transition: all 0.3s ease;
            cursor: pointer;
        }
        .tool-card:hover {
            border-color: #667eea;
            box-shadow: 0 4px 12px rgba(102, 126, 234, 0.1);
            background: white;
        }
        .tool-name {
            font-weight: 600;
            color: #667eea;
            font-size: 1.1rem;
            margin-bottom: 0.5rem;
            font-family: "Courier New", monospace;
        }
        .tool-desc {
            color: #555;
            font-size: 0.95rem;
            line-height: 1.5;
            margin-bottom: 1rem;
        }
        .tool-info {
            display: flex;
            gap: 1rem;
            flex-wrap: wrap;
            font-size: 0.85rem;
        }
        .tool-badge {
            background: #e0e0e0;
            color: #333;
            padding: 0.3rem 0.8rem;
            border-radius: 20px;
        }
        .tool-badge.write {
            background: #ffebee;
            color: #c62828;
        }
        footer {
            background: #f5f5f5;
            padding: 2rem;
            text-align: center;
            color: #666;
            border-top: 1px solid #e0e0e0;
        }
        .endpoints {
            background: #f0f4ff;
            padding: 1.5rem;
            border-radius: 8px;
            margin-bottom: 2rem;
            font-size: 0.95rem;
        }
        .endpoints strong {
            color: #667eea;
        }
        code {
            background: #f5f5f5;
            padding: 0.2rem 0.5rem;
            border-radius: 4px;
            font-family: "Courier New", monospace;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🛠️ K8s DevOps Tool Catalog</h1>
            <p>Unified API for Kubernetes investigation, diagnosis, and remediation</p>
            <div class="stats">
"""

    total_tools = sum(len(v) for v in categories.values())
    html += f"""
                <div class="stat">
                    <div class="stat-number">{total_tools}</div>
                    <div class="stat-label">Total Tools</div>
                </div>
                <div class="stat">
                    <div class="stat-number">{len(categories)}</div>
                    <div class="stat-label">Categories</div>
                </div>
                <div class="stat">
                    <div class="stat-number">32</div>
                    <div class="stat-label">MCP Registered</div>
                </div>
            </div>
        </header>

        <div class="content">
            <div class="endpoints">
                <strong>📡 Available Endpoints:</strong><br>
                • <code>GET /docs</code> — OpenAPI Swagger UI<br>
                • <code>GET /openapi.json</code> — API contract<br>
                • <code>POST /mcp</code> — MCP Streamable HTTP transport<br>
                • <code>GET /debug/tools</code> — JSON tool list<br>
                • <code>POST /debug/call</code> — Direct tool invocation
            </div>
"""

    for category_key in sorted(categories.keys()):
        tools = categories[category_key]
        title = category_titles.get(category_key, category_key)
        
        html += f'<div class="category">\n'
        html += f'  <div class="category-title">{title}</div>\n'
        html += f'  <div class="tools-grid">\n'

        for tool in sorted(tools, key=lambda t: t.name):
            is_write = any(x in tool.name for x in ["delete", "exec", "rollout", "scale", "apply"])
            write_badge = '<span class="tool-badge write">⚠️ WRITE</span>' if is_write else ''
            
            # Escape HTML in description
            desc = tool.description.replace("<", "&lt;").replace(">", "&gt;")[:200]
            if len(tool.description or "") > 200:
                desc += "..."

            html += f"""    <div class="tool-card">
      <div class="tool-name">{tool.name}</div>
      <div class="tool-desc">{desc}</div>
      <div class="tool-info">
        {write_badge}
      </div>
    </div>
"""

        html += '  </div>\n</div>\n'

    html += """
        </div>

        <footer>
            <p>🔗 <strong>API Docs:</strong> <code>GET /docs</code> | <strong>OpenAPI:</strong> <code>GET /openapi.json</code></p>
            <p style="margin-top: 1rem; font-size: 0.9rem;">Updated """ + datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC") + """</p>
        </footer>
    </div>
</body>
</html>
"""
    return html


# ── ASGI mount for the MCP transport ──────────────────────────────────────────

class StreamableHTTPMount:
    """Thin ASGI shim that forwards MCP-path requests to the session manager."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # pragma: no cover
        request = Request(scope, receive)
        _authorize_request(request, self.app.state.auth_token)
        manager: StreamableHTTPSessionManager | None = self.app.state.session_manager
        if manager is None:
            resp = JSONResponse({"detail": "MCP session manager not initialized"}, status_code=503)
            await resp(scope, receive, send)
            return
        # region agent log
        _agent_debug_log(
            "http_mcp/http_server.py:StreamableHTTPMount.__call__",
            "MCP streamable handler entered",
            {
                "method": scope.get("method"),
                "path": scope.get("path"),
                "rootPath": scope.get("root_path"),
                "rawPath": scope.get("raw_path", b"").decode("utf-8", "replace"),
                "accept": request.headers.get("accept"),
                "contentType": request.headers.get("content-type"),
                "hasMcpSessionId": bool(request.headers.get("mcp-session-id")),
                "managerInitialized": manager is not None,
            },
            "H3,H4",
        )
        # endregion
        try:
            await manager.handle_request(scope, receive, send)
        except Exception as exc:
            # region agent log
            _agent_debug_log(
                "http_mcp/http_server.py:StreamableHTTPMount.__call__",
                "MCP streamable handler raised",
                {
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "errorType": type(exc).__name__,
                    "error": str(exc)[:500],
                },
                "H3",
            )
            # endregion
            raise


# ── App factory ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the shared MCP server and HTTP session manager on app startup."""
    try:
        log_runtime_settings(logger)
    except ValueError as e:
        logger.error("Configuration error: %s", e)
        raise

    server, tool_count = await build_server("mcp-http")
    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=app.state.http_config.json_response,
        stateless=app.state.http_config.stateless,
    )

    app.state.mcp_server = server
    app.state.tool_count = tool_count
    app.state.session_manager = session_manager

    logger.info(
        "HTTP MCP server ready — %s:%s%s — %s tools registered",
        app.state.http_config.host,
        app.state.http_config.port,
        app.state.http_config.mcp_path,
        tool_count,
    )

    async with session_manager.run():
        yield


def create_app(http_config: HTTPConfig | None = None) -> FastAPI:
    """Return a configured FastAPI app exposing the MCP toolset over Streamable HTTP."""
    http_config = http_config or HTTPConfig()
    http_config.mcp_path = _normalize_mcp_path(http_config.mcp_path)

    app = FastAPI(
        title="K8s DevOps MCP Server (HTTP)",
        description="Streamable HTTP transport for the shared K8s DevOps MCP toolset.",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.http_config = http_config
    app.state.auth_token = http_config.auth_token
    app.state.mcp_server = None
    app.state.tool_count = 0
    app.state.session_manager = None

    # ── Informational root ────────────────────────────────────────────────────

    @app.get("/")
    async def root():
        cfg = app.state.http_config
        return {
            "name": "K8s DevOps MCP Server (HTTP)",
            "transport": "streamable-http",
            "protocol": "MCP March 2025+",
            "mcp_endpoint": cfg.mcp_path,
            "endpoints": {
                "GET /health": "Health check",
                "GET /docs": "OpenAPI Swagger UI",
                "GET /openapi.json": "API contract (OpenAPI 3.0)",
                "GET /tools/catalog": "Interactive tool browser (HTML or JSON)",
                "GET /tools/categories": "Category metadata only",
                "GET /debug/tools": "Raw tool list (smoke test)",
                "POST /debug/call": "Direct tool invocation (smoke test)",
                f"POST {cfg.mcp_path}": "MCP Streamable HTTP transport",
            },
            "auth_enabled": bool(cfg.auth_token),
            "quick_start": {
                "browse_tools": f"http://{cfg.host}:{cfg.port}/tools/catalog",
                "swagger_docs": f"http://{cfg.host}:{cfg.port}/docs",
                "api_contract": f"http://{cfg.host}:{cfg.port}/openapi.json",
            },
            "cursor_config_example": {
                "mcpServers": {
                    "k8s-devops-http": {
                        "type": "http",
                        "url": f"http://{cfg.host}:{cfg.port}{cfg.mcp_path}",
                    }
                }
            },
        }

    # ── Health check ──────────────────────────────────────────────────────────

    @app.get("/health")
    async def health_check():
        cfg = app.state.http_config
        return {
            "status": "ok",
            "transport": "streamable-http",
            "protocol": "MCP March 2025+",
            "mcp_server": "running" if app.state.mcp_server else "not initialized",
            "tools_count": app.state.tool_count,
            "mcp_endpoint": cfg.mcp_path,
            "auth_enabled": bool(cfg.auth_token),
        }

    # ── Debug endpoints (not part of MCP protocol) ────────────────────────────

    @app.get("/debug/tools")
    async def debug_list_tools(request: Request):
        """List all registered tools — useful for local smoke tests."""
        _authorize_request(request, app.state.auth_token)
        server: Server | None = app.state.mcp_server
        if server is None:
            return {"tools": []}
        tools = await list_registered_tools(server)
        return {
            "tools_count": len(tools),
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.inputSchema,
                }
                for t in tools
            ],
        }

    @app.post("/debug/call")
    async def debug_call_tool(request: Request, body: DebugToolCallRequest):
        """Call a tool directly without going through the MCP protocol — useful for smoke tests."""
        _authorize_request(request, app.state.auth_token)
        server: Server | None = app.state.mcp_server
        if server is None:
            raise HTTPException(status_code=503, detail="MCP server not initialized")
        try:
            handler = server.request_handlers.get(types.CallToolRequest)
            if handler is None:
                raise RuntimeError("MCP call handler is not registered")
            mcp_request = types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name=body.tool_name,
                    arguments=body.arguments,
                )
            )
            server_result = await handler(mcp_request)
            result = server_result.root
        except Exception as e:
            logger.error("Error calling tool %s: %s", body.tool_name, e, exc_info=True)
            return JSONResponse(
                {"success": False, "tool": body.tool_name, "error": str(e)},
                status_code=500,
            )

        return {
            "success": not getattr(result, "isError", False),
            "tool": body.tool_name,
            "isError": getattr(result, "isError", False),
            "results": [
                block.model_dump(mode="json") if hasattr(block, "model_dump") else block
                for block in getattr(result, "content", [])
            ],
            "structuredContent": getattr(result, "structuredContent", None),
        }

    # ── Tool Catalog ──────────────────────────────────────────────────────────

    @app.get("/tools/catalog")
    async def get_tools_catalog(request: Request, format: str = "html"):
        """
        Human-readable catalog of all available MCP tools.

        Query Parameters:
          - format: 'html' (default) for interactive browser, 'json' for machine-readable format

        Returns interactive HTML or JSON with tools organized by category.
        """
        _authorize_request(request, app.state.auth_token)
        server: Server | None = app.state.mcp_server
        if server is None:
            raise HTTPException(status_code=503, detail="MCP server not initialized")

        tools = await list_registered_tools(server)
        categories = _categorize_tools(tools)

        if format == "json":
            return {
                "metadata": {
                    "total_tools": len(tools),
                    "categories_count": len(categories),
                    "last_updated": datetime.now().isoformat(),
                    "mcp_protocol": "March 2025+",
                },
                "categories": {
                    category_name: [
                        {
                            "name": t.name,
                            "description": t.description,
                            "inputSchema": t.inputSchema,
                        }
                        for t in cat_tools
                    ]
                    for category_name, cat_tools in categories.items()
                }
            }

        else:  # html
            html = _render_catalog_html(categories)
            return HTMLResponse(content=html)

    @app.get("/tools/categories")
    async def get_tools_categories(request: Request):
        """Get just the category metadata without tool details."""
        _authorize_request(request, app.state.auth_token)
        server: Server | None = app.state.mcp_server
        if server is None:
            raise HTTPException(status_code=503, detail="MCP server not initialized")

        tools = await list_registered_tools(server)
        categories = _categorize_tools(tools)

        category_info = {
            "kubectl_discovery": "🔍 Discovery — Find workloads & resources",
            "kubectl_details": "📋 Details — Get deep resource info",
            "kubectl_context": "🔄 Context — Manage kubeconfig",
            "kubectl_repo": "📦 Repository — Search deployment configs",
            "kubectl_analysis": "📊 Analysis — Complex investigations",
            "kubectl_recovery": "🔧 Recovery — Remediation ops (WRITE)",
            "ai_analysis": "🤖 AI Analysis — provider-backed diagnostics",
        }

        return {
            "total_tools": len(tools),
            "categories": {
                cat: {
                    "description": category_info.get(cat, cat),
                    "tool_count": len(tools_list),
                    "tools": [t.name for t in tools_list]
                }
                for cat, tools_list in categories.items()
            }
        }

    # ── Mount MCP transport ───────────────────────────────────────────────────
    app.mount(http_config.mcp_path, StreamableHTTPMount(app))

    return app


# Module-level app instance (used by uvicorn when launched via `python http_server.py`)
app = create_app()


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    """Parse CLI args and start the HTTP MCP server."""
    defaults = HTTPConfig()

    parser = ArgumentParser(description="K8s DevOps MCP Server — Streamable HTTP transport")
    parser.add_argument("--host", default=defaults.host,
                        help="Host to bind to (default: 127.0.0.1; use 0.0.0.0 for network access)")
    parser.add_argument("--port", type=int, default=defaults.port,
                        help="Port to listen on (default: 8001)")
    parser.add_argument("--mcp-path", default=defaults.mcp_path,
                        help="HTTP path for the MCP transport (default: /mcp)")
    parser.add_argument("--auth-token", default=defaults.auth_token,
                        help="Optional bearer token — clients must send Authorization: Bearer <token>")
    parser.add_argument("--json-response", action="store_true",
                        help="Return JSON bodies instead of SSE streams")
    parser.add_argument("--stateless", action="store_true",
                        help="Disable server-side session tracking (one-shot requests)")
    parser.add_argument("--reload", action="store_true",
                        help="Enable uvicorn auto-reload (development only)")
    args = parser.parse_args()

    cfg = HTTPConfig(
        host=args.host,
        port=args.port,
        mcp_path=args.mcp_path,
        auth_token=args.auth_token,
        json_response=args.json_response,
        stateless=args.stateless,
    )

    logger.info("Starting K8s DevOps MCP HTTP server")
    logger.info("  Health:      http://%s:%s/health", cfg.host, cfg.port)
    logger.info("  Debug tools: http://%s:%s/debug/tools", cfg.host, cfg.port)
    logger.info("  MCP (HTTP):  http://%s:%s%s", cfg.host, cfg.port, _normalize_mcp_path(cfg.mcp_path))
    logger.info("  Auth:        %s", "enabled" if cfg.auth_token else "disabled")

    uvicorn.run(
        create_app(cfg),
        host=cfg.host,
        port=cfg.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
