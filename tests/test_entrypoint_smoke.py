from __future__ import annotations

import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def test_entrypoint_serves_over_stdio() -> None:
    # Launch the package as `python -m data_aggregator_mcp` and drive a real
    # MCP initialize + list_tools handshake over stdio — proves the packaged
    # entry point actually serves, not just imports.
    params = StdioServerParameters(command=sys.executable, args=["-m", "data_aggregator_mcp"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
    assert init.serverInfo.name == "data-aggregator-mcp"
    names = {t.name for t in tools.tools}
    assert names == {"search", "resolve", "fetch", "list_sources", "operate"}
