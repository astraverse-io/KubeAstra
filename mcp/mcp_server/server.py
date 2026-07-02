"""Unified K8s DevOps MCP Server.

Merges capabilities from:
  - mcp-k8s-investigation-agent: live kubectl investigation, multi-cluster, recovery ops
  - k8s-ansible-mcp: Gemini AI error analysis, RAG similarity search, runbook generation

Cursor Configuration (~/.cursor/mcp.json):
{
  "mcpServers": {
    "k8s-devops": {
      "command": "/Users/pruthvidavineni/AI_DevOps_Assistant/mcp/venv/bin/python",
      "args": ["/Users/pruthvidavineni/AI_DevOps_Assistant/mcp/mcp_server/server.py"],
      "env": {
        "PYTHONPATH": "/Users/pruthvidavineni/AI_DevOps_Assistant/mcp",
        "ALLOWED_NAMESPACES": "prod,staging,dev,default"
      }
    }
  }
}
"""

import asyncio
import logging
import sys
from pathlib import Path

_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

from mcp.server.stdio import stdio_server

from mcp_server.runtime import build_server, log_runtime_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("k8s_devops_mcp.log"),
        logging.StreamHandler(sys.stderr),
    ],
)

logger = logging.getLogger(__name__)


async def main():
    logger.info("Starting K8s DevOps MCP Server (unified)")

    try:
        log_runtime_settings(logger)
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)

    server, tool_count = await build_server("mcp")
    logger.info("Registered %s tools", tool_count)

    logger.info("Server ready, waiting for connections via stdio...")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception:
        logger.exception("Server error")
        sys.exit(1)
