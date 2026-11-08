"""Agents built with other frameworks, hosted as AIOS agents (FR-AGT-02, FR-INT-04; WP 3.6).

An Agent manifest names the framework as its runtime, and its entrypoint the framework's object:

    runtime: {type: "adapter:langgraph", entrypoint: "my_app.graph:build"}
    runtime: {type: "adapter:openai-agents", entrypoint: "my_app.agents:build"}

The agent is then an AIOS agent like any other: it gets tasks for its capabilities, its runs are
traced, budgeted and replayable, and the governance holds because of two rules:

* **Every model call goes through the Model Kernel.** The adapter hands the framework a model that
  is ``ctx.model``, so routing, budgets, data classes and recordings apply.
* **AIOS tools are called through Tool OS.** A tool a framework is given by the adapter is
  ``ctx.tools``, so policy, approvals and egress rules apply. A tool written in the agent's own
  code is the agent's own code, as in any Python agent.

Each framework is an optional extra: ``pip install "aios[langgraph]"`` or
``"aios[openai-agents]"``.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from aios.core.manifests import AgentManifest
from aios.kernel import Context

Handler = Callable[[Context, Any], Awaitable[Any]]

ADAPTERS = {
    "langgraph": "aios.adapters.langgraph",
    "openai-agents": "aios.adapters.openai_agents",
}
EXTRAS = {"langgraph": "aios[langgraph]", "openai-agents": "aios[openai-agents]"}

_current: ContextVar[Context | None] = ContextVar("aios_adapter_context", default=None)


def current() -> Context:
    """The task's ``ctx``, while an adapter runs a framework's agent for it."""
    found = _current.get()
    if found is None:
        raise RuntimeError("no AIOS task is running: AIOS models and tools work inside one")
    return found


def handler_for(manifest: AgentManifest) -> Handler | None:
    """The handler that runs an adapter agent, or ``None`` for an agent of another runtime."""
    runtime = manifest.spec.runtime
    if not runtime.type.startswith("adapter:"):
        return None
    framework = runtime.type.removeprefix("adapter:")
    module_name = ADAPTERS.get(framework)
    if module_name is None:
        known = ", ".join(sorted(ADAPTERS))
        raise ValueError(f"no adapter for {framework!r}; there are adapters for {known}")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ImportError(
            f'{framework} isn\'t installed: pip install "{EXTRAS[framework]}"'
        ) from exc
    target = load_object(str(runtime.entrypoint))
    make: Callable[[Any, AgentManifest], Handler] = module.handler
    return make(target, manifest)


def load_object(entrypoint: str) -> Any:
    """``module.path:attribute``: a framework's agent or graph, or a function that builds one."""
    module_name, _, attribute = entrypoint.partition(":")
    return getattr(importlib.import_module(module_name), attribute)


def offered_tools(manifest: AgentManifest) -> list[str]:
    """The AIOS tools an adapter offers the framework: the exact names the agent may use. A
    pattern (``search.>``) allows a family, but names no tool to describe to a model."""
    return [name for name in manifest.spec.tools.allow if "*" not in name and ">" not in name]


class Running:
    """``with Running(ctx):`` makes ``ctx`` the one AIOS models and tools use."""

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def __enter__(self) -> Context:
        self._token = _current.set(self.ctx)
        return self.ctx

    def __exit__(self, *exc: object) -> None:
        _current.reset(self._token)
