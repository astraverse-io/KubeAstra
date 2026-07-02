import sys
import os

# add mcp to sys.path
sys.path.insert(0, os.path.abspath("mcp"))

from tool_registry import resolve_tool, dispatch, DispatchContext
import asyncio

async def main():
    get_alerts = resolve_tool("get_recent_alerts")
    print(get_alerts.name)
    ctx = DispatchContext(surface="mcp", allow_write=False)
    res = dispatch("get_recent_alerts", {"limit": 10}, ctx)
    print("Alerts:", res)
    
if __name__ == "__main__":
    asyncio.run(main())
