"""The ``Context`` an agent handler receives (requirements §7.5).

Every ``ctx.*`` call goes through a port (model, tools, memory) or the fabric (emit), is
counted against the agent's limits and the task's budget (FR-AGT-08), carries the task's
run, delegation and trace context, and is recorded as events by the component that serves it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, Literal, cast, overload

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from aios.core.catalog import TaskBudget, Usage
from aios.core.envelope import Event
from aios.core.errors import BudgetExceededError, ModelOutputError
from aios.core.ports import (
    CallSite,
    MemoryGateway,
    Message,
    ModelGateway,
    ModelRequest,
    ModelResponse,
    ToolGateway,
)
from aios.fabric.port import EventBus
from aios.kernel.definitions import AgentDefinition


class Budget:
    """Usage counted against the agent's limits and the task's budget."""

    def __init__(self, definition: AgentDefinition, task: TaskBudget | None) -> None:
        limits = definition.limits
        self.max_steps = _tightest(limits.max_steps, task.max_steps if task else None)
        self.max_tokens = _tightest(limits.max_tokens, task.max_tokens if task else None)
        self.max_cost = _tightest(limits.max_cost_usd, task.max_cost_usd if task else None)
        self.steps = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0

    @property
    def usage(self) -> Usage:
        return Usage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=round(self.cost_usd, 8),
        )

    def step(self) -> None:
        """Count one model or tool call; refuse it once the step budget is spent."""
        if self.max_steps is not None and self.steps >= self.max_steps:
            raise BudgetExceededError("steps", self.max_steps, self.steps + 1)
        self.check()
        self.steps += 1

    def add(self, usage: Usage) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cost_usd += usage.cost_usd
        self.check()

    def check(self) -> None:
        tokens = self.input_tokens + self.output_tokens
        if self.max_tokens is not None and tokens > self.max_tokens:
            raise BudgetExceededError("tokens", self.max_tokens, tokens)
        if self.max_cost is not None and self.cost_usd > self.max_cost:
            raise BudgetExceededError("cost", self.max_cost, round(self.cost_usd, 8))


def _tightest(*limits: float | int | None) -> Any:
    present = [limit for limit in limits if limit is not None]
    return min(present) if present else None


class _Sites:
    def __init__(self, context: Context) -> None:
        self._context = context
        self.counts = {"model": 0, "tool": 0, "memory": 0}

    def next(self, kind: Literal["model", "tool", "memory"]) -> CallSite:
        context = self._context
        index = self.counts[kind]
        self.counts[kind] = index + 1
        return CallSite(
            event=context.event,
            agent=context.agent,
            principal=context.principal,
            index=index,
            step=context.step,
            item=context.item,
            loop=context.loop,
            attempt=context.attempt,
            tainted=context.tainted,
        )


class ModelAccess:
    """``ctx.model``: model calls routed by the Model Kernel's policy (FR-MOD-03)."""

    def __init__(self, context: Context, gateway: ModelGateway) -> None:
        self._context = context
        self._gateway = gateway

    @overload
    async def generate[T](
        self,
        *,
        messages: list[Message] | str,
        output: type[T],
        task: str | None = None,
        system: str | None = None,
        max_tokens: int = 16_000,
        temperature: float | None = None,
    ) -> T: ...

    @overload
    async def generate(
        self,
        *,
        messages: list[Message] | str,
        output: None = None,
        task: str | None = None,
        system: str | None = None,
        max_tokens: int = 16_000,
        temperature: float | None = None,
    ) -> str: ...

    async def generate(
        self,
        *,
        messages: list[Message] | str,
        output: type[Any] | None = None,
        task: str | None = None,
        system: str | None = None,
        max_tokens: int = 16_000,
        temperature: float | None = None,
    ) -> Any:
        """Generate a completion. With ``output``, the result is validated structured output."""
        context = self._context
        context.budget.step()
        adapter = TypeAdapter[Any](output) if output is not None else None
        request = ModelRequest(
            task=task or context.definition.model_task,
            system=system,
            messages=[Message(role="user", content=messages)]
            if isinstance(messages, str)
            else messages,
            output_schema=adapter.json_schema() if adapter is not None else None,
            output_name=getattr(output, "__name__", None),
            max_tokens=max_tokens,
            temperature=temperature,
            data_class=context.event.dataclass,
        )
        response = await self._gateway.generate(request, context.sites.next("model"))
        context.responses.append(response)
        context.budget.add(response.usage)
        if adapter is None:
            return response.text
        return _structured(adapter, response)


def _structured(adapter: TypeAdapter[Any], response: ModelResponse) -> Any:
    try:
        if response.parsed is not None:
            return adapter.validate_python(response.parsed)
        return adapter.validate_python(json.loads(_strip_fences(response.text)))
    except (ValidationError, ValueError) as exc:
        raise ModelOutputError(f"{response.provider}/{response.model}: {exc}") from exc


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0]
    return stripped


class ToolAccess:
    """``ctx.tools``: tool calls through the Tool OS pipeline (FR-TOOL-05)."""

    def __init__(self, context: Context, gateway: ToolGateway) -> None:
        self._context = context
        self._gateway = gateway

    async def invoke(
        self, name: str, *, idempotency_key: str | None = None, **arguments: JsonValue
    ) -> JsonValue:
        context = self._context
        context.budget.step()
        result = await self._gateway.invoke(
            name, arguments, context.sites.next("tool"), idempotency_key=idempotency_key
        )
        if result.untrusted:
            context.tainted = True  # FR-SEC-09: later calls carry the taint
        return result.output


class MemoryAccess:
    """``ctx.memory`` (Memory OS arrives in WP 2.1; Phase 1 recalls nothing)."""

    def __init__(self, context: Context, gateway: MemoryGateway) -> None:
        self._context = context
        self._gateway = gateway

    async def recall(
        self, query: str, *, scopes: list[str] | None = None, budget_tokens: int = 2_000
    ) -> str:
        context = self._context
        return await self._gateway.recall(
            query,
            scopes=scopes or ["session"],
            budget_tokens=budget_tokens,
            site=context.sites.next("memory"),
        )


class Context:
    """What an agent handler can do. Created by the kernel for every delivery."""

    def __init__(
        self,
        *,
        event: Event,
        definition: AgentDefinition,
        bus: EventBus,
        models: ModelGateway,
        tools: ToolGateway,
        memory: MemoryGateway,
        instance: str,
        logger: logging.Logger,
    ) -> None:
        self.event = event
        self.definition = definition
        self.instance = instance
        self._bus = bus
        data: Mapping[str, Any] = (
            cast(Mapping[str, Any], event.data) if isinstance(event.data, dict) else {}
        )
        self.step: str | None = data.get("step")
        self.item: int | None = data.get("item")
        self.loop: int = data.get("loop", 0)
        self.attempt: int = data.get("attempt", 1)
        budget = data.get("budget")
        self.budget = Budget(definition, TaskBudget.model_validate(budget) if budget else None)
        self.tainted = False
        self.responses: list[ModelResponse] = []
        self.sites = _Sites(self)
        self.model = ModelAccess(self, models)
        self.tools = ToolAccess(self, tools)
        self.memory = MemoryAccess(self, memory)
        self.log = logging.LoggerAdapter(
            logger,
            {"run_id": event.runid, "agent": definition.name, "step": self.step},
        )

    @property
    def agent(self) -> str:
        return self.definition.name

    @property
    def tenant(self) -> str:
        return self.event.tenantid

    @property
    def app(self) -> str:
        return self.event.appid

    @property
    def run_id(self) -> str | None:
        return self.event.runid

    @property
    def principal(self) -> str:
        return self.definition.principal(self.tenant)

    @property
    def source(self) -> str:
        return f"aios://{self.tenant}/apps/{self.app}/agents/{self.agent}"

    @property
    def usage(self) -> Usage:
        return self.budget.usage

    @staticmethod
    def user(text: str) -> Message:
        return Message(role="user", content=text)

    @staticmethod
    def assistant(text: str) -> Message:
        return Message(role="assistant", content=text)

    def step_key(self, name: str) -> str:
        """An idempotency key for a side effect of this step attempt (NFR-06)."""
        return f"{self.run_id or self.event.id}/{self.step or '-'}/{self.attempt}/{name}"

    async def emit(self, type: str, data: Any = None) -> Event:
        """Publish a fact caused by the event being handled."""
        payload: JsonValue = (
            cast(JsonValue, data.model_dump(mode="json"))
            if isinstance(data, BaseModel)
            else cast(JsonValue, data)
        )
        event = self.event.derive(type, payload, source=self.source, principal=self.principal)
        await self._bus.publish(event)
        return event
