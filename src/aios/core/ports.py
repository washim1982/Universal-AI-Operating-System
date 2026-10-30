"""Ports between runtime components (ADR-001: components never import each other).

The Agent Kernel hands agents a ``Context`` whose ``model``, ``tools`` and ``memory`` calls go
through these interfaces; the Model Kernel, Tool OS and Memory OS implement them, and the
access layer wires them together. Every call carries a :class:`CallSite`, which says which
task it belongs to, so that calls are recorded against the run, attributed and budgeted, and
replayed instead of repeated after a crash (NFR-06).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from aios.core.catalog import Usage
from aios.core.envelope import DataClass, Event


@dataclass(frozen=True, slots=True)
class CallSite:
    """Where a model or tool call happens: the task being executed and the call's position."""

    event: Event
    """The event the agent is handling (``task.requested`` or a subscribed fact)."""
    agent: str
    principal: str
    index: int
    """0-based position of this call among the calls of its kind in this execution."""
    step: str | None = None
    item: int | None = None
    loop: int = 0
    attempt: int = 1
    tainted: bool = False
    """The agent's context already contains untrusted content (FR-SEC-09)."""
    tags: Mapping[str, str] = field(default_factory=dict[str, str])

    def key(self, kind: Literal["model", "tool", "memory"]) -> str:
        """A stable identifier of the call site, independent of models, tools and content.

        ``research#2/research-agent/l0a1/model/0`` is the first model call of the research
        agent on the third fan-out item, first loop, first attempt. Cassettes replay by it.
        """
        step = self.step or self.event.type
        item = f"#{self.item}" if self.item is not None else ""
        return f"{step}{item}/{self.agent}/l{self.loop}a{self.attempt}/{kind}/{self.index}"


# --------------------------------------------------------------------------- models


class Message(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["user", "assistant"]
    content: str


class ModelRequest(BaseModel):
    """A provider-neutral model call (FR-MOD-01)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task: str | None = Field(default=None, description="Routing hint for the model policy")
    system: str | None = None
    messages: list[Message] = Field(min_length=1)
    output_schema: dict[str, Any] | None = Field(
        default=None, description="JSON Schema for structured output"
    )
    output_name: str | None = None
    max_tokens: int = Field(default=16_000, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    data_class: DataClass = DataClass.INTERNAL


class ModelResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    parsed: JsonValue = None
    provider: str
    model: str
    usage: Usage
    latency_ms: float = 0.0
    stop_reason: str | None = None
    replayed: bool = Field(
        default=False, description="Taken from the run's recorded calls, not executed again"
    )


class ModelGateway(ABC):
    @abstractmethod
    async def generate(self, request: ModelRequest, site: CallSite) -> ModelResponse: ...


# --------------------------------------------------------------------------- tools


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    output: JsonValue
    provider: str
    untrusted: bool = Field(default=True, description="Tool output is untrusted data (FR-SEC-09)")
    latency_ms: float = 0.0


class ToolGateway(ABC):
    @abstractmethod
    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, JsonValue],
        site: CallSite,
        *,
        idempotency_key: str | None = None,
    ) -> ToolResult: ...


# --------------------------------------------------------------------------- memory


class MemoryGateway(ABC):
    """Memory OS port (WP 2.1). Phase 1 ships :class:`NoMemory`."""

    @abstractmethod
    async def recall(
        self, query: str, *, scopes: list[str], budget_tokens: int, site: CallSite
    ) -> str: ...


class NoMemory(MemoryGateway):
    async def recall(
        self, query: str, *, scopes: list[str], budget_tokens: int, site: CallSite
    ) -> str:
        return ""
