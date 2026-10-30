"""Agent definitions: what the kernel needs to host an agent (FR-AGT-01, 02).

An agent is an async function ``handler(ctx, payload)``. Its name, capabilities, subscriptions
and limits come from its Agent manifest, from the :func:`agent` decorator, or both (the
manifest wins, and the decorator supplies the Python types).
"""

from __future__ import annotations

import importlib
import inspect
import typing
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, cast

from pydantic import TypeAdapter

from aios.core.envelope import Event
from aios.core.manifests import AgentManifest, Limits, ToolAccess
from aios.core.topics import validate_name, validate_pattern

Handler = Callable[..., Awaitable[Any]]
_MARKER = "__aios_agent__"


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    name: str
    handler: Handler
    capabilities: tuple[str, ...] = ()
    subscriptions: tuple[str, ...] = ()
    """Fact patterns the agent reacts to (choreography)."""
    version: str = "0.0.0"
    limits: Limits = field(default_factory=Limits)
    tools: ToolAccess = field(default_factory=ToolAccess)
    model_task: str | None = None
    """Default routing hint for ``ctx.model.generate`` (``spec.model.task``)."""
    input_type: Any = Any
    output_type: Any = Any

    @property
    def principal_name(self) -> str:
        return self.name

    def principal(self, tenant: str) -> str:
        return f"agent:{self.name}@{tenant}"

    def parse_input(self, data: Any) -> Any:
        if self.input_type is Any:
            return data
        if self.input_type is Event:
            raise TypeError("event handlers receive the event, not its data")
        return TypeAdapter[Any](self.input_type).validate_python(data)

    def dump_output(self, value: Any) -> Any:
        if self.output_type is Any or value is None:
            return TypeAdapter[Any](cast(Any, type(value))).dump_python(value, mode="json")
        return TypeAdapter[Any](self.output_type).dump_python(value, mode="json")


def agent(
    name: str | None = None,
    *,
    capability: str | Sequence[str] | None = None,
    subscribe: str | Sequence[str] | None = None,
) -> Callable[[Handler], Handler]:
    """Mark an async function as an agent handler (requirements §7.5).

    ``capability`` handlers receive ``(ctx, task_input)`` and their return value is published as
    ``task.completed``; ``subscribe`` handlers receive ``(ctx, event)`` for every matching fact.
    """
    capabilities = (capability,) if isinstance(capability, str) else tuple(capability or ())
    subscriptions = (subscribe,) if isinstance(subscribe, str) else tuple(subscribe or ())
    for value in capabilities:
        validate_name(value, "capability")
    for value in subscriptions:
        validate_pattern(value, "subscription")

    def mark(handler: Handler) -> Handler:
        if not inspect.iscoroutinefunction(handler):
            raise TypeError(f"agent handler {handler.__qualname__} must be an async function")
        input_type, output_type = _signature_types(handler)
        definition = AgentDefinition(
            name=name or handler.__name__.replace("_", "-"),
            handler=handler,
            capabilities=capabilities,
            subscriptions=subscriptions,
            input_type=input_type,
            output_type=output_type,
        )
        setattr(handler, _MARKER, definition)
        return handler

    return mark


def definition_of(handler: Handler) -> AgentDefinition:
    """The definition attached by :func:`agent`, or a bare one for an undecorated function."""
    marked = getattr(handler, _MARKER, None)
    if isinstance(marked, AgentDefinition):
        return marked
    input_type, output_type = _signature_types(handler)
    return AgentDefinition(
        name=handler.__name__.replace("_", "-"),
        handler=handler,
        input_type=input_type,
        output_type=output_type,
    )


def from_manifest(manifest: AgentManifest, handler: Handler | None = None) -> AgentDefinition:
    """Combine an Agent manifest with its Python handler (imported from the entrypoint)."""
    spec = manifest.spec
    if handler is None:
        if spec.runtime.type != "python" or spec.runtime.entrypoint is None:
            raise ValueError(
                f"agent {manifest.metadata.name!r}: only python runtimes are hosted in-process "
                "until WP 3.x (containers, A2A)"
            )
        handler = load_entrypoint(spec.runtime.entrypoint)
    base = definition_of(handler)
    return replace(
        base,
        name=manifest.metadata.name,
        version=manifest.metadata.version,
        capabilities=tuple(c.name for c in spec.capabilities),
        subscriptions=tuple(spec.subscriptions),
        limits=spec.limits,
        tools=spec.tools,
        model_task=spec.model.task,
    )


def load_entrypoint(entrypoint: str) -> Handler:
    module_name, _, attribute = entrypoint.partition(":")
    module = importlib.import_module(module_name)
    handler = getattr(module, attribute)
    if not callable(handler):
        raise TypeError(f"{entrypoint} is not callable")
    return cast(Handler, handler)


def _signature_types(handler: Handler) -> tuple[Any, Any]:
    parameters = list(inspect.signature(handler).parameters)
    if len(parameters) != 2:
        raise TypeError(f"agent handler {handler.__qualname__} must take (ctx, input)")
    try:
        hints = typing.get_type_hints(handler)
    except (NameError, TypeError):  # annotations that only exist for type checkers
        hints = {}
    return hints.get(parameters[1], Any), hints.get("return", Any)
