"""Synthetic MCP echo for transport-only diagnostic; no memory/authentication claims."""
from __future__ import annotations

import io
import json
import os
import sys

import anyio
from mcp import types
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server


async def tools(context: ServerRequestContext[None], params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[types.Tool(name="synthetic_echo", input_schema={"type": "object"})])


async def echo(context: ServerRequestContext[None], params: types.CallToolRequestParams) -> types.CallToolResult:
    if params.name != "synthetic_echo" or params.arguments != {"text": "合成IPC"}:
        raise ValueError("synthetic_request_only")
    value = {"text": "合成响应" * 1024}
    return types.CallToolResult(structured_content=value,
        content=[types.TextContent(type="text", text=json.dumps(value, ensure_ascii=False))])


async def main() -> None:
    server: Server[None] = Server("evernook-synthetic-echo", version="1.0.0", on_list_tools=tools, on_call_tool=echo)
    server.middleware.clear()
    with io.TextIOWrapper(os.fdopen(os.dup(sys.stdin.fileno()), "rb"), encoding="utf-8", errors="strict") as reader:
        async with stdio_server(stdin=anyio.wrap_file(reader)) as (incoming, outgoing):
            await server.run(incoming, outgoing, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
