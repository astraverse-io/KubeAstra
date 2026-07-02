"""Small Streamable HTTP client example for the K8s DevOps MCP server."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def mcp_http_session(url: str, auth_token: str | None = None):
    """Open an MCP ClientSession over Streamable HTTP."""
    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    async with httpx.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(url, http_client=http_client) as streams:
            async with ClientSession(*streams[:2]) as session:
                await session.initialize()
                yield session


async def main() -> None:
    parser = argparse.ArgumentParser(description="Test the K8s DevOps HTTP MCP server")
    parser.add_argument("--url", default="http://127.0.0.1:8001/mcp/", help="Streamable HTTP MCP endpoint")
    parser.add_argument("--auth-token", default=None, help="Optional bearer token")
    parser.add_argument("--tool", default=None, help="Optional tool name to call after listing tools")
    parser.add_argument(
        "--args-json",
        default="{}",
        help="JSON object with tool arguments, used with --tool",
    )
    args = parser.parse_args()

    async with mcp_http_session(args.url, args.auth_token) as session:
        tools = await session.list_tools()
        logger.info("Connected. Server exposed %s tools.", len(tools.tools))
        logger.info("First tools: %s", [tool.name for tool in tools.tools[:10]])

        if args.tool:
            tool_args: dict[str, Any] = json.loads(args.args_json)
            result = await session.call_tool(args.tool, tool_args)
            logger.info("Tool result: %s", result.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
