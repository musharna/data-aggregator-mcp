from __future__ import annotations

import os
import sys

import jsonschema
import pytest
from mcp import ClientSession, MCPError
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import INVALID_PARAMS

from data_aggregator_mcp import tool_specs


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
        unknown_arg = await session.call_tool("search", {"query": "x", "limit": 3})
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
    # An argument the tool does not declare is refused over the wire, not dropped (X-M2).
    assert unknown_arg.is_error is True
    assert "'limit'" in unknown_arg.content[0].text


_LIVE = os.environ.get("DATA_AGGREGATOR_MCP_LIVE") == "1"


async def test_the_wire_serves_tool_specs_and_answers_within_its_schemas() -> None:
    """Real execution for tool_specs: what a client lists over stdio IS tool_specs.TOOLS,
    a real call's answer validates against the outputSchema advertised for it, and a
    prompt missing a required argument is the spec's -32602 (it rendered a hole). The
    child gets this process's environment, so it serves the code under test."""
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "data_aggregator_mcp"], env=dict(os.environ)
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        listed = await session.call_tool("list_sources", {})
        searched = (
            await session.call_tool("search", {"query": "rice", "sources": ["zenodo"], "size": 2})
            if _LIVE
            else None
        )
        rendered = await session.get_prompt("find_data", {"topic": "maize"})
        refusals = []
        for name, args in (("find_data", {}), ("find_data", {"topic": "  "}), ("nope", {})):
            with pytest.raises(MCPError) as refused:
                await session.get_prompt(name, args)
            refusals.append((refused.value.code, refused.value.message))
    assert [t.model_dump() for t in tools] == [t.model_dump() for t in tool_specs.TOOLS]
    schemas = {t.name: t.output_schema for t in tools}
    assert listed.is_error is False
    jsonschema.validate(listed.structured_content, schemas["list_sources"])
    if searched is not None:
        assert searched.is_error is False, searched.content
        assert searched.structured_content["count"] >= 1
        jsonschema.validate(searched.structured_content, schemas["search"])
    assert rendered.messages[0].content.text.startswith(
        "Use the data-aggregator `search` tool to find datasets about: maize."
    )
    assert refusals == [
        (INVALID_PARAMS, "prompt 'find_data' requires ['topic']"),
        (INVALID_PARAMS, "prompt 'find_data' requires ['topic']"),
        (INVALID_PARAMS, "unknown prompt: 'nope'"),
    ]
