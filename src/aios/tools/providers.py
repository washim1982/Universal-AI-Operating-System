"""Tool providers: where tool calls are executed (FR-TOOL-04; FR-INT-01).

A provider lists :class:`ToolSpec` entries and executes calls. Native tools are decorated
Python functions; MCP servers are imported over Streamable HTTP or stdio with the official
``mcp`` SDK. The registry resolves qualified names (``<provider>.<resource>.<action>``) to the
provider bound for them.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from typing import Any, cast

import mcp
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, create_model

from aios.core.errors import ToolError, ToolInputError
from aios.core.manifests import ToolName
from aios.core.policy import SideEffect


class ToolSpec(BaseModel):
    """A registered tool (FR-TOOL-01)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: ToolName
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] | None = None
    side_effect: SideEffect
    version: str = "1"
    timeout_s: float = Field(default=30.0, gt=0)
    trusted: bool = Field(
        default=False, description="Output is trusted data (native, internal tools only)"
    )

    @property
    def provider(self) -> str:
        return self.name.split(".", 1)[0]

    @property
    def description_hash(self) -> str:
        """Pins what the model is told about the tool, against rug-pull changes (FR-TOOL-09)."""
        canonical = json.dumps(
            {"description": self.description, "input": self.input_schema}, sort_keys=True
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


class ToolProvider(ABC):
    name: str
    replayable: bool = False
    """Calls may be served from a cassette in replay mode (network-backed read tools)."""

    @abstractmethod
    async def list_tools(self) -> list[ToolSpec]: ...

    @abstractmethod
    async def call(
        self, name: str, arguments: Mapping[str, JsonValue], *, idempotency_key: str | None
    ) -> JsonValue: ...

    async def close(self) -> None:  # noqa: B027 - optional hook
        """Release connections."""


# --------------------------------------------------------------------------- native tools

ToolFunction = Callable[..., Awaitable[Any]]


class NativeTool:
    def __init__(self, spec: ToolSpec, function: ToolFunction, arguments: type[BaseModel]) -> None:
        self.spec = spec
        self.function = function
        self.arguments = arguments
        parameters = inspect.signature(function).parameters
        self.takes_idempotency_key = "idempotency_key" in parameters


def tool(
    name: str,
    *,
    side_effect: SideEffect,
    description: str | None = None,
    timeout_s: float = 30.0,
    trusted: bool = False,
) -> Callable[[ToolFunction], NativeTool]:
    """Declare a native tool: an async function whose parameters become its input schema."""

    def wrap(function: ToolFunction) -> NativeTool:
        if not inspect.iscoroutinefunction(function):
            raise TypeError(f"tool {name} must be an async function")
        hints = {k: v for k, v in inspect.get_annotations(function, eval_str=True).items()}
        fields: dict[str, Any] = {}
        for parameter in inspect.signature(function).parameters.values():
            if parameter.name == "idempotency_key":
                continue
            default = ... if parameter.default is inspect.Parameter.empty else parameter.default
            fields[parameter.name] = (hints.get(parameter.name, Any), default)
        arguments = create_model(f"{function.__name__}_arguments", **fields)
        output = hints.get("return")
        spec = ToolSpec(
            name=name,
            description=description or inspect.getdoc(function) or "",
            input_schema=arguments.model_json_schema(),
            output_schema=TypeAdapter[Any](output).json_schema()
            if output not in (None, Any)
            else None,
            side_effect=side_effect,
            timeout_s=timeout_s,
            trusted=trusted,
        )
        return NativeTool(spec, function, arguments)

    return wrap


class NativeProvider(ToolProvider):
    def __init__(self, name: str, tools: Sequence[NativeTool], *, replayable: bool = False) -> None:
        self.name = name
        self.replayable = replayable
        self._tools = {native.spec.name: native for native in tools}

    async def list_tools(self) -> list[ToolSpec]:
        return [native.spec for native in self._tools.values()]

    async def call(
        self, name: str, arguments: Mapping[str, JsonValue], *, idempotency_key: str | None
    ) -> JsonValue:
        native = self._tools[name]
        try:
            parsed = native.arguments.model_validate(dict(arguments))
        except ValueError as exc:
            raise ToolInputError(f"{name}: {exc}") from exc
        kwargs = parsed.model_dump()
        if native.takes_idempotency_key:
            kwargs["idempotency_key"] = idempotency_key
        result = await native.function(**kwargs)
        adapter = TypeAdapter[Any](cast(Any, type(result)))
        return cast(JsonValue, adapter.dump_python(result, mode="json"))


# --------------------------------------------------------------------------- MCP

_NAME_TOKEN = re.compile(r"[^a-z0-9_-]+")


def _unwrapped(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema of the value inside an MCP ``{"result": ...}`` structured-output wrapper."""
    properties = schema.get("properties", {})
    if set(properties) == {"result"} and schema.get("required") == ["result"]:
        inner = dict(properties["result"])
        if "$defs" in schema:
            inner["$defs"] = schema["$defs"]
        return inner
    return schema


def qualified_name(provider: str, mcp_name: str) -> str:
    """The AIOS name for an MCP tool: kept if it is already ``<provider>.<x>.<y>``."""
    tokens = [_NAME_TOKEN.sub("-", t.lower()).strip("-") or "tool" for t in mcp_name.split(".")]
    if len(tokens) == 3 and tokens[0] == provider:
        return ".".join(tokens)
    if len(tokens) == 2:
        return ".".join([provider, *tokens])
    return f"{provider}.mcp.{'-'.join(tokens)}"


class MCPProvider(ToolProvider):
    """An MCP server's tools (FR-INT-01), over Streamable HTTP (a URL) or stdio (a command)."""

    def __init__(
        self,
        name: str,
        server: str | mcp.StdioServerParameters,
        *,
        side_effects: Mapping[str, SideEffect] | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self.name = name
        self.server = server
        self.side_effects = dict(side_effects or {})
        self.timeout_s = timeout_s
        self._stack: AsyncExitStack | None = None
        self._client: mcp.Client | None = None
        self._names: dict[str, str] = {}  # AIOS name -> MCP name

    async def _connect(self) -> mcp.Client:
        if self._client is None:
            # "auto" negotiates the 2026 protocol and is meant to fall back to the classic
            # initialize handshake; some servers drop the connection on its probe instead, so
            # retry once with the handshake ("legacy") before giving up.
            for mode in ("auto", "legacy"):
                stack = AsyncExitStack()
                try:
                    self._client = await stack.enter_async_context(
                        mcp.Client(self.server, mode=mode, read_timeout_seconds=self.timeout_s)
                    )
                except Exception as exc:
                    await stack.aclose()
                    if mode == "legacy":
                        raise ToolError(f"can't connect to MCP server {self.name}: {exc}") from exc
                    continue
                self._stack = stack
                break
        assert self._client is not None  # noqa: S101 - set by the loop or it raised
        return self._client

    async def list_tools(self) -> list[ToolSpec]:
        client = await self._connect()
        listing = await client.list_tools()
        specs: list[ToolSpec] = []
        for listed in listing.tools:
            name = qualified_name(self.name, listed.name)
            self._names[name] = listed.name
            annotations = listed.annotations
            if name in self.side_effects:
                side_effect = self.side_effects[name]
            elif annotations is not None and annotations.read_only_hint:
                side_effect = SideEffect.READ
            elif annotations is not None and annotations.destructive_hint:
                side_effect = SideEffect.DESTRUCTIVE
            else:
                side_effect = SideEffect.WRITE  # unknown effects are treated as writes
            specs.append(
                ToolSpec(
                    name=name,
                    description=listed.description or "",
                    input_schema=dict(listed.input_schema),
                    output_schema=_unwrapped(dict(listed.output_schema))
                    if listed.output_schema
                    else None,
                    side_effect=side_effect,
                    timeout_s=self.timeout_s,
                )
            )
        return specs

    async def call(
        self, name: str, arguments: Mapping[str, JsonValue], *, idempotency_key: str | None
    ) -> JsonValue:
        client = await self._connect()
        if not self._names:
            await self.list_tools()
        result = await client.call_tool(self._names.get(name, name), dict(arguments))
        texts = [getattr(block, "text", "") for block in result.content if hasattr(block, "text")]
        if result.is_error:
            raise ToolError(f"{name}: {' '.join(texts) or 'the MCP server reported an error'}")
        structured = result.structured_content
        if structured is not None:
            # Servers wrap non-object results as {"result": ...} (MCP structured output).
            if list(structured) == ["result"]:
                return cast(JsonValue, structured["result"])
            return cast(JsonValue, structured)
        parsed: list[JsonValue] = []
        for text in texts:  # a list result arrives as one text block per item
            try:
                parsed.append(cast(JsonValue, json.loads(text)))
            except ValueError:
                return "\n".join(texts)
        if len(parsed) == 1 and len(result.content) == 1:
            return parsed[0]
        return parsed

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._client = None
