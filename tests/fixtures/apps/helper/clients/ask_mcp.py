"""Use an AIOS server's MCP tools with the MCP SDK's own client (testing guide, Part 22).

    python ask_mcp.py                                  # the tools and resources
    python ask_mcp.py ask-helper "hello"               # ask the helper App
    python ask_mcp.py run-status helper <run-id>       # how a run stands

``AIOS_SERVER`` is where AIOS is (``http://127.0.0.1:18080`` if unset) and ``AIOS_API_KEY`` the
key to use; ``aios dev`` needs none. It speaks the 2026 protocol, and the classic one if that fails,
as some security software breaks the 2026 handshake on the way (see the testing guide).
"""

import argparse
import asyncio
import json
import os
from typing import Any

import httpx2
import mcp
from mcp.client.streamable_http import streamable_http_client


async def run(server: str, headers: dict[str, str], mode: str, args: argparse.Namespace) -> None:
    async with httpx2.AsyncClient(headers=headers, timeout=300) as http:
        transport = streamable_http_client(f"{server}/mcp", http_client=http)
        async with mcp.Client(transport, mode=mode, read_timeout_seconds=300) as client:
            if args.tool is None:
                for tool in (await client.list_tools()).tools:
                    print(f"tool {tool.name}: {tool.description}")
                for resource in (await client.list_resources()).resources:
                    print(f"resource {resource.uri}")
                return
            arguments: dict[str, Any]
            if args.tool == "run-status":
                app, run_id = args.values
                arguments = {"app": app, "run_id": run_id, "wait_seconds": 30}
            else:
                arguments = {"message": " ".join(args.values)}
            result = await client.call_tool(args.tool, arguments)
            texts = [getattr(block, "text", "") for block in result.content]
            print(("error: " if result.is_error else "") + " ".join(texts))
            if result.structured_content:
                print(json.dumps(result.structured_content, indent=2))


async def main() -> None:
    parser = argparse.ArgumentParser(description="Use an AIOS server's MCP tools")
    parser.add_argument("tool", nargs="?", help="ask-<app> or run-status")
    parser.add_argument("values", nargs="*", help="the message; or the App and the run id")
    args = parser.parse_args()
    server = os.environ.get("AIOS_SERVER", "http://127.0.0.1:18080").rstrip("/")
    key = os.environ.get("AIOS_API_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        await run(server, headers, "auto", args)
    except Exception as exc:
        print(f"(the 2026 handshake failed: {type(exc).__name__}; using the classic one)")
        await run(server, headers, "legacy", args)


if __name__ == "__main__":
    asyncio.run(main())
