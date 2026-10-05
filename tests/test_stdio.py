"""End-to-end: start the server over stdio and list its tools via a real MCP client."""

import asyncio
import os
import sys

import pytest

mcp_client = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

EXPECTED = {
    "list_databases", "list_collections", "collection_schema", "collection_indexes", "find",
    "count", "distinct", "aggregate", "explain", "collection_stats", "db_stats", "readonly_status",
}


def test_tools_over_stdio():
    async def run():
        env = {**os.environ, "MONGODB_URI": "mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=200"}
        params = StdioServerParameters(command=sys.executable, args=["-m", "mongodb_readonly_mcp.server"], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = (await s.list_tools()).tools
                assert {t.name for t in tools} == EXPECTED
                assert all(t.annotations.model_dump(by_alias=True)["readOnlyHint"] for t in tools)
                # The guard fires before any network I/O
                res = await s.call_tool("aggregate", {"database": "x", "collection": "y", "pipeline": [{"$out": "z"}]})
                assert res.model_dump(by_alias=True)["isError"] and "$out" in res.content[0].text

    asyncio.run(run())
