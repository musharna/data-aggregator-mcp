from __future__ import annotations

import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def test_entrypoint_serves_over_stdio() -> None:
    # Launch the package as `python -m data_aggregator_mcp` and drive a real
    # MCP initialize + list_tools handshake over stdio — proves the packaged
    # entry point actually serves, not just imports.
    params = StdioServerParameters(command=sys.executable, args=["-m", "data_aggregator_mcp"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        tools = await session.list_tools()
        # The tool-layer contract mcp 1.x gave for free and 2.x does not: a
        # refusal raised inside the handler reaches the client as an error
        # RESULT carrying its text, not as a JSON-RPC error (which the client
        # raises as MCPError, and which on modern-era connections carries only
        # a generic message). Positive control in the same session: a
        # legitimate call succeeds and carries structured content.
        refused = await session.call_tool("fetch", {"id": "bioproject:PRJNA111"})
        ok = await session.call_tool("list_sources", {})
        bad_args = await session.call_tool("search", {"size": "not-an-int"})
    assert init.server_info.name == "data-aggregator-mcp"
    names = {t.name for t in tools.tools}
    assert names == {"search", "resolve", "fetch", "list_sources", "operate", "relate"}
    assert refused.is_error is True
    assert "bioproject:PRJNA111" in refused.content[0].text
    assert "has no wired fetch backend" in refused.content[0].text
    assert ok.is_error is False
    assert any(s["name"] == "zenodo" for s in ok.structured_content["sources"])
    # Input validation against inputSchema was the 1.x decorator's; it is ours now.
    assert bad_args.is_error is True
    assert bad_args.content[0].text.startswith("Input validation error:")
