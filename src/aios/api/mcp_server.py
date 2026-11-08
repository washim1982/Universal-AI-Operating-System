"""AIOS Apps for MCP clients: an MCP server at ``/mcp`` (FR-INT-01 P1; WP 3.7).

An MCP client, such as an AI assistant or an IDE, connects over Streamable HTTP with an AIOS
credential (``Authorization: Bearer <API key or token>``) and sees:

* a tool ``ask-<app>`` for each App it can chat with: a run of that App, for the caller, which
  answers with the run's answer, or says what the run waits for;
* ``run-status``, to ask again about a run that was still working or waiting;
* each App's description, as the resource ``aios://apps/<app>``.

Every call is the caller's own: their roles, the App's policies, budgets and approvals apply,
and the run is traced like any other. There is no tool to approve or deny: a person decides,
in the console, in a channel or through the API, not an assistant on their behalf.

The server is stateless, so any API replica can answer any request. Without sign-in (a local
development server) it only answers requests addressed to a loopback name, so that a web page
can't reach it through DNS rebinding.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from fastapi import HTTPException
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from aios import __version__
from aios.controlplane import Runtime, views
from aios.controlplane.asking import Asked, chat_apps, outcome, start
from aios.core.errors import NotFoundError, PolicyDeniedError
from aios.core.identity import Identity
from aios.core.ids import is_ulid
from aios.core.policy import PolicyAction

ASK_PREFIX = "ask-"
STATUS_TOOL = "run-status"
RESOURCE_PREFIX = "aios://apps/"
IDENTITY_KEY = "aios.identity"
"""Where the authenticated caller waits in the request's scope, for the MCP handlers."""

Identify = Callable[[str | None, str | None], Awaitable[Identity]]

INSTRUCTIONS = (
    "Each ask- tool sends a message to an AIOS App and returns its answer. A run can wait for "
    "a person (an approval or a task) or take longer than one call: the reply then says so, "
    "with the run's id, and run-status asks about it again later. People decide approvals "
    "themselves; no tool here decides one."
)

LOOPBACK = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"],
    allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
)


def _caller(ctx: Any) -> Identity:
    identity = ctx.request.scope.get(IDENTITY_KEY) if ctx.request is not None else None
    if not isinstance(identity, Identity):  # only reachable through the authenticated door
        raise MCPError(types.INVALID_REQUEST, "sign in first")
    return identity


def _ask_tool(app: str, description: str) -> types.Tool:
    about = description.strip() or f"The {app} App."
    return types.Tool(
        name=f"{ASK_PREFIX}{app}",
        title=f"Ask {app}",
        description=(
            f"{about} Sends a message to the AIOS App {app} and returns its answer, or what "
            "its run waits for."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "What to ask"},
                "session": {
                    "type": "string",
                    "description": "An earlier run's session id, to go on with its conversation",
                },
            },
            "required": ["message"],
            "additionalProperties": False,
        },
        annotations=types.ToolAnnotations(
            title=f"Ask {app}", read_only_hint=False, destructive_hint=False, open_world_hint=True
        ),
    )


STATUS = types.Tool(
    name=STATUS_TOOL,
    title="Run status",
    description=(
        "How an AIOS run stands: its answer once it has one, or what it waits for. Use it for "
        "a run an ask- tool said was still working or waiting."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "app": {"type": "string", "description": "The App the run belongs to"},
            "run_id": {"type": "string", "description": "The run's id"},
            "wait_seconds": {
                "type": "number",
                "minimum": 0,
                "description": "Wait this long for the run to end or wait for someone",
            },
        },
        "required": ["app", "run_id"],
        "additionalProperties": False,
    },
    annotations=types.ToolAnnotations(title="Run status", read_only_hint=True),
)


def _answered(asked: Asked) -> types.CallToolResult:
    text = asked.say(f"ask again with {STATUS_TOOL}, app {asked.app}, run_id {asked.run_id}")
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content=asked.view(),
        is_error=asked.status in ("failed", "cancelled"),
    )


def _failed(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], is_error=True
    )


def mcp_server(runtime: Runtime, *, wait_seconds: float) -> Server[Any]:
    async def authorized(identity: Identity, action: PolicyAction, app: str | None = None) -> None:
        decision = await runtime.authorize(identity, action, **({"app": app} if app else {}))
        if not decision.allowed:
            raise PolicyDeniedError(decision.reason or f"{action.value} is not allowed")

    async def list_tools(ctx: Any, _params: Any) -> types.ListToolsResult:
        identity = _caller(ctx)
        try:
            await authorized(identity, PolicyAction.APP_READ)
        except PolicyDeniedError as exc:
            raise MCPError(types.INVALID_REQUEST, str(exc)) from exc
        apps = chat_apps(runtime, identity.tenant)
        return types.ListToolsResult(
            tools=[*(_ask_tool(app, about) for app, about in apps.items()), STATUS]
        )

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        identity = _caller(ctx)
        arguments: dict[str, Any] = dict(params.arguments or {})
        try:
            if params.name == STATUS_TOOL:
                return await status(identity, arguments)
            if params.name.startswith(ASK_PREFIX):
                return await asking(identity, params.name.removeprefix(ASK_PREFIX), arguments)
        except PolicyDeniedError as exc:
            return _failed(f"not allowed: {exc}")
        except NotFoundError as exc:
            return _failed(str(exc))
        return _failed(f"no tool {params.name!r}")

    async def asking(
        identity: Identity, app: str, arguments: dict[str, Any]
    ) -> types.CallToolResult:
        await authorized(identity, PolicyAction.RUN_START, app)
        if app not in chat_apps(runtime, identity.tenant):
            return _failed(f"no App {app!r} to ask")
        message, session = arguments.get("message"), arguments.get("session")
        if not isinstance(message, str) or not message:
            return _failed("message is the text to send the App")
        if session is not None and not (isinstance(session, str) and is_ulid(session)):
            return _failed("session is an earlier run's session id")
        run_id = await start(
            runtime, identity, app, message, session=session, via="mcp", authorized=True
        )
        return _answered(
            await outcome(runtime, identity, app, run_id, timeout=wait_seconds, authorized=True)
        )

    async def status(identity: Identity, arguments: dict[str, Any]) -> types.CallToolResult:
        app, run_id = arguments.get("app"), arguments.get("run_id")
        wait = arguments.get("wait_seconds", 0)
        if not isinstance(app, str) or not isinstance(run_id, str) or not is_ulid(run_id):
            return _failed("app is an App's name, and run_id one of its runs")
        if not isinstance(wait, int | float) or wait < 0:
            return _failed("wait_seconds is a number of seconds, 0 or more")
        asked = await outcome(
            runtime, identity, app, run_id, timeout=min(float(wait), wait_seconds)
        )
        return _answered(asked)

    async def list_resources(ctx: Any, _params: Any) -> types.ListResourcesResult:
        identity = _caller(ctx)
        try:
            await authorized(identity, PolicyAction.APP_READ)
        except PolicyDeniedError as exc:
            raise MCPError(types.INVALID_REQUEST, str(exc)) from exc
        return types.ListResourcesResult(
            resources=[
                types.Resource(
                    name=app,
                    title=f"The {app} App",
                    uri=f"{RESOURCE_PREFIX}{app}",
                    description=about or None,
                    mime_type="application/json",
                )
                for app, about in chat_apps(runtime, identity.tenant).items()
            ]
        )

    async def read_resource(
        ctx: Any, params: types.ReadResourceRequestParams
    ) -> types.ReadResourceResult:
        identity = _caller(ctx)
        uri = str(params.uri)
        try:
            await authorized(identity, PolicyAction.APP_READ)
            if not uri.startswith(RESOURCE_PREFIX):
                raise NotFoundError(f"no resource {uri}")
            installed = runtime.app(uri.removeprefix(RESOURCE_PREFIX), tenant=identity.tenant)
        except (PolicyDeniedError, NotFoundError) as exc:
            raise MCPError(types.INVALID_PARAMS, str(exc)) from exc
        text = json.dumps(views.app_of(installed), indent=2, default=str)
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=uri, mime_type="application/json", text=text)]
        )

    return Server(
        "aios",
        version=__version__,
        title="AIOS",
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_resources=list_resources,
        on_read_resource=read_resource,
    )


class MCPEndpoint:
    """The ``/mcp`` door: who is calling first, as for every other route, then MCP."""

    def __init__(
        self, runtime: Runtime, identify: Identify, *, wait_seconds: float, local: bool
    ) -> None:
        self.identify = identify
        self.manager = StreamableHTTPSessionManager(
            app=mcp_server(runtime, wait_seconds=wait_seconds),
            stateless=True,
            json_response=False,  # server-sent events, the SDK's default and clients' usual
            security_settings=LOOPBACK if local else None,
        )

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncGenerator[None]:
        async with self.manager.run():
            yield

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = Headers(scope=scope)
        try:
            identity = await self.identify(
                headers.get("authorization"), headers.get("x-aios-tenant")
            )
        except HTTPException as exc:
            refused = JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
            )
            await refused(scope, receive, send)
            return
        await self.manager.handle_request({**scope, IDENTITY_KEY: identity}, receive, send)


__all__ = ["MCPEndpoint", "mcp_server"]
