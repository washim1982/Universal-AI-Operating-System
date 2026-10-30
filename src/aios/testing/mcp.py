"""A real MCP server for tests, over Streamable HTTP on a free local port (FR-INT-01).

async with fake_mcp_server(tools=["search.web.query"]) as server:
    runtime.register_tool_provider("search", mcp_url=server.url)
    ...
    assert server.calls
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import uvicorn
from mcp.server.mcpserver import MCPServer
from pydantic import JsonValue

DEFAULT_RESULTS: list[dict[str, str]] = [
    {
        "title": "Semantic conventions for generative AI systems",
        "url": "https://opentelemetry.io/docs/specs/semconv/gen-ai/",
        "snippet": "Conventions for GenAI spans, metrics and events.",
    },
    {
        "title": "OpenTelemetry GenAI SIG notes",
        "url": "https://github.com/open-telemetry/semantic-conventions/issues?q=gen-ai",
        "snippet": "Discussion of agent and tool-call attributes.",
    },
]


@dataclass
class FakeMCPServer:
    url: str
    calls: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextlib.asynccontextmanager
async def fake_mcp_server(
    tools: Sequence[str] = ("search.web.query",),
    *,
    responses: Mapping[str, Callable[[dict[str, Any]], Any]] | None = None,
) -> AsyncGenerator[FakeMCPServer]:
    """Serve ``tools``; each returns ``responses[name](arguments)`` or canned search results."""
    port = _free_port()
    fake = FakeMCPServer(url=f"http://127.0.0.1:{port}/mcp")
    server: MCPServer[Any] = MCPServer("aios-fake-mcp")
    for name in tools:

        def register(tool_name: str) -> None:
            async def handler(q: str = "", limit: int = 5, url: str = "") -> JsonValue:
                arguments: dict[str, Any] = {"q": q, "limit": limit, "url": url}
                fake.calls.append((tool_name, arguments))
                respond = (responses or {}).get(tool_name)
                result = respond(arguments) if respond else DEFAULT_RESULTS[:limit]
                return cast(JsonValue, result)

            server.tool(name=tool_name, description=f"Fake {tool_name}", structured_output=True)(
                handler
            )

        register(name)
    config = uvicorn.Config(
        server.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning"
    )
    web = uvicorn.Server(config)
    task = asyncio.create_task(web.serve())
    try:
        for _ in range(500):
            if web.started:
                break
            await asyncio.sleep(0.01)
        else:
            raise RuntimeError("the fake MCP server did not start")
        yield fake
    finally:
        web.should_exit = True
        await task
