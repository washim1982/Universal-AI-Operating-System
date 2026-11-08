"""The ``Context`` an agent handler receives (requirements §7.5).

Every ``ctx.*`` call goes through a port (model, tools, memory) or the fabric (emit), is
counted against the agent's limits and the task's budget (FR-AGT-08), carries the task's
run, delegation and trace context, and is recorded as events by the component that serves it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any, Literal, cast, overload

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from aios.core.catalog import EmitStubbed, ModelToolCall, TaskBudget, Usage
from aios.core.envelope import DataClass, Event
from aios.core.errors import (
    AwaitingApproval,
    BudgetExceededError,
    ModelOutputError,
    ReservedEventError,
)
from aios.core.memory import (
    ContextPack,
    Erasure,
    MemoryHit,
    MemoryItem,
    MemoryKind,
    MemoryScope,
    Retention,
    SearchMode,
)
from aios.core.ports import (
    CallSite,
    EmbeddingRequest,
    InjectionClassifier,
    MemoryGateway,
    Message,
    ModelGateway,
    ModelRequest,
    ModelResponse,
    ModelTool,
    ToolGateway,
    model_tool_name,
    screened,
    tool_name_of,
)
from aios.core.replay import holding_back
from aios.core.taint import quoted
from aios.core.topics import RESERVED_DOMAINS
from aios.fabric.port import EventBus, Scope
from aios.kernel.definitions import AgentDefinition

MAX_TAINT_SOURCES = 20


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
        self.counts = {"model": 0, "tool": 0, "memory": 0, "emit": 0}

    def next(self, kind: Literal["model", "tool", "memory", "emit"]) -> CallSite:
        site = self.peek(kind)
        self.counts[kind] += 1
        return site

    def peek(self, kind: Literal["model", "tool", "memory", "emit"]) -> CallSite:
        """The next site, without taking it: for what is asked about, not done."""
        context = self._context
        index = self.counts[kind]
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
            taint=tuple(context.taint_sources),
            data_class=context.data_class,
            executions=context.executions,
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
            data_class=context.data_class,
        )
        response = await self._gateway.generate(request, context.sites.next("model"))
        context.responses.append(response)
        context.budget.add(response.usage)
        if adapter is None:
            return response.text
        return _structured(adapter, response)

    async def complete(
        self,
        messages: list[Message] | str,
        *,
        tools: Sequence[ModelTool] = (),
        tool_choice: Literal["auto", "required", "none"] | None = None,
        task: str | None = None,
        system: str | None = None,
        output_schema: dict[str, Any] | None = None,
        max_tokens: int = 16_000,
        temperature: float | None = None,
    ) -> ModelResponse:
        """One model turn, as it came: its text and any tool calls it asks for (FR-MOD-01).

        Asking is all the model does. Whether to call a tool is the agent's choice, and the call
        goes through ``ctx.tools`` like any other, so policy, budgets and approvals apply.
        """
        context = self._context
        context.budget.step()
        request = ModelRequest(
            task=task or context.definition.model_task,
            system=system,
            messages=[Message(role="user", content=messages)]
            if isinstance(messages, str)
            else messages,
            output_schema=output_schema,
            max_tokens=max_tokens,
            temperature=temperature,
            data_class=context.data_class,
            tools=list(tools),
            tool_choice=tool_choice,
        )
        response = await self._gateway.generate(request, context.sites.next("model"))
        context.responses.append(response)
        context.budget.add(response.usage)
        return response

    async def embed(self, texts: list[str] | str, *, task: str = "embedding") -> list[list[float]]:
        """One vector per text, from the model the policy routes ``task`` to."""
        context = self._context
        context.budget.step()
        request = EmbeddingRequest(
            task=task,
            texts=[texts] if isinstance(texts, str) else texts,
            data_class=context.data_class,
        )
        response = await self._gateway.embed(request, context.sites.next("model"))
        context.budget.add(response.usage)
        return response.vectors


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
        self,
        name: str,
        *,
        idempotency_key: str | None = None,
        rationale: str | None = None,
        **arguments: JsonValue,
    ) -> JsonValue:
        """Call a tool. ``rationale`` is why, in the agent's words, for whoever has to approve it.

        A call that needs an approval stops this task. Once someone answers, the task runs again
        from the top: what it already did replays, and this line returns, or raises
        :class:`~aios.core.errors.ApprovalDeniedError`, which an agent can catch to answer some
        other way. Only ``ctx`` calls replay, so anything else a handler does before this line
        happens again.
        """
        context = self._context
        context.budget.step()
        try:
            result = await self._gateway.invoke(
                name,
                arguments,
                context.sites.next("tool"),
                idempotency_key=idempotency_key,
                rationale=rationale,
            )
        except AwaitingApproval as waiting:
            context.awaiting = waiting.approval
            raise
        await context.admit(result.output, f"tool:{name}", untrusted=result.untrusted)
        return result.output

    async def describe(self, *names: str) -> list[ModelTool]:
        """The tools ``names``, as a model is told about them (``ctx.model.complete(tools=...)``).

        A model sees ``search.web.query`` as ``search__web__query``: providers allow no dots.
        """
        found: list[ModelTool] = []
        for name in names:
            described = await self._gateway.describe(name, self._context.sites.peek("tool"))
            found.append(
                ModelTool(
                    name=model_tool_name(described.name),
                    description=described.description,
                    parameters=dict(described.input_schema),
                )
            )
        return found

    async def call(
        self,
        call: ModelToolCall,
        *,
        idempotency_key: str | None = None,
        rationale: str | None = None,
    ) -> JsonValue:
        """Make a call a model asked for, through Tool OS like any other."""
        return await self.invoke(
            tool_name_of(call.name),
            idempotency_key=idempotency_key,
            rationale=rationale,
            **call.arguments,
        )


class MemoryAccess:
    """``ctx.memory`` (§7.7): the memory of this run, its user, this agent and this App.

    Whatever an agent reads is untrusted and keeps its label, so its context takes on both
    (FR-MEM-13, FR-SEC-11). An embedding's cost counts against the task's budget; a memory call
    isn't a step, because steps limit what the model and tools can do.
    """

    def __init__(self, context: Context, gateway: MemoryGateway) -> None:
        self._context = context
        self._gateway = gateway

    async def _absorb(
        self, usage: Usage, read: Sequence[tuple[str, DataClass]], content: object
    ) -> None:
        context = self._context
        context.budget.add(usage)
        if not read:
            return
        context.data_class = DataClass.strictest(context.data_class, *(label for _, label in read))
        for item, _ in read:
            context.taint(f"memory:{item}")
        await context.admit(content, "memory", untrusted=False)  # tainted above, by item

    async def recall(
        self,
        query: str,
        *,
        scopes: list[MemoryScope] | None = None,
        budget_tokens: int = 2_000,
    ) -> ContextPack:
        """What a model should know for ``query``: cited, deduplicated and within the budget."""
        pack = await self._gateway.recall(
            query,
            scopes=scopes or ["session"],
            budget_tokens=budget_tokens,
            site=self._context.sites.next("memory"),
        )
        await self._absorb(
            pack.usage, [(cited.id, pack.sensitivity) for cited in pack.citations], pack.content
        )
        return pack

    async def search(
        self,
        query: str,
        *,
        scopes: list[MemoryScope] | None = None,
        k: int = 5,
        filters: Mapping[str, Any] | None = None,
        mode: SearchMode = "hybrid",
    ) -> list[MemoryHit]:
        """Ranked items, without assembling a context pack."""
        result = await self._gateway.search(
            query,
            scopes=scopes or ["session"],
            k=k,
            filters=filters,
            mode=mode,
            site=self._context.sites.next("memory"),
        )
        read = [(hit.item.id, hit.item.sensitivity) for hit in result.hits]
        await self._absorb(result.usage, read, [hit.item.content for hit in result.hits])
        return result.hits

    async def put(
        self,
        content: str,
        *,
        scope: MemoryScope,
        subject: str | None = None,
        kind: MemoryKind = "semantic",
        retention: Retention | None = None,
        ttl: timedelta | None = None,
        sensitivity: DataClass | None = None,
        importance: float = 0.5,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> str:
        """Store an item in ``scope``; returns its id. It can't be labelled below what was read."""
        written = await self._gateway.put(
            content,
            scope=scope,
            subject=subject,
            kind=kind,
            retention=retention,
            ttl=ttl,
            sensitivity=sensitivity,
            importance=importance,
            metadata=metadata,
            site=self._context.sites.next("memory"),
        )
        self._context.budget.add(written.usage)
        return written.item.id

    async def get(self, item_id: str) -> MemoryItem | None:
        item = await self._gateway.get(item_id, site=self._context.sites.next("memory"))
        if item is not None:
            await self._absorb(Usage(), [(item.id, item.sensitivity)], item.content)
        return item

    async def update(
        self,
        item_id: str,
        *,
        content: str | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
        importance: float | None = None,
        version: int | None = None,
    ) -> MemoryItem:
        """The next version of an item. ``version`` refuses to overwrite someone else's change."""
        written = await self._gateway.update(
            item_id,
            content=content,
            metadata=metadata,
            importance=importance,
            version=version,
            site=self._context.sites.next("memory"),
        )
        self._context.budget.add(written.usage)
        return written.item

    async def forget(self, *, item: str | None = None, subject: str | None = None) -> Erasure:
        return await self._gateway.forget(
            item=item, subject=subject, site=self._context.sites.next("memory")
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
        classifier: InjectionClassifier | None = None,
    ) -> None:
        self.event = event
        self.definition = definition
        self.instance = instance
        self._bus = bus
        self._classifier = classifier
        data: Mapping[str, Any] = (
            cast(Mapping[str, Any], event.data) if isinstance(event.data, dict) else {}
        )
        self.step: str | None = data.get("step")
        self.item: int | None = data.get("item")
        self.loop: int = data.get("loop", 0)
        self.attempt: int = data.get("attempt", 1)
        budget = data.get("budget")
        self.budget = Budget(definition, TaskBudget.model_validate(budget) if budget else None)
        self._tainted = event.tainted
        self.taint_sources: list[str] = list(data.get("taint") or ())
        """Where the run's untrusted content came from, for whoever has to judge it."""
        self.data_class = event.dataclass
        """The strictest label of what this task has read, which is what it can reveal."""
        self.executions: tuple[str, ...] = tuple(data.get("resumes") or ())
        self.awaiting: str | None = None
        """The approval this task can't go on without, however the handler took the news."""
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
    def tainted(self) -> bool:
        """Untrusted content has reached this context. It can't be taken back (FR-SEC-09)."""
        return self._tainted

    def taint(self, source: str) -> None:
        self._tainted = True
        if source not in self.taint_sources and len(self.taint_sources) < MAX_TAINT_SOURCES:
            self.taint_sources.append(source)

    async def admit(self, content: object, source: str, *, untrusted: bool = True) -> None:
        """Content from outside is about to reach this agent: note it, and have it looked at.

        A classifier can taint what came through a trusted door, and nothing else depends on it.
        """
        if untrusted:
            self.taint(source)
        suspected = await screened(self._classifier, content, source)
        if suspected is not None:
            self.taint(f"suspected:{source}")
            await self._publish_once("security.injection.suspected", suspected)

    @staticmethod
    def quote(text: str, *, source: str) -> str:
        """Outside content, marked as data, for a prompt. See :func:`aios.core.taint.quoted`."""
        return quoted(text, source=source)

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
        """An idempotency key for a side effect of this step (NFR-06).

        Every attempt shares it, so a retry can't do twice what the first attempt already did.
        """
        item = f"#{self.item}" if self.item is not None else ""
        return f"{self.run_id or self.event.id}/{self.step or '-'}{item}/l{self.loop}/{name}"

    async def emit(self, type: str, data: Any = None) -> Event:
        """Publish a fact caused by the event being handled.

        Platform domains are refused: an agent that could emit ``approval.granted`` or
        ``task.completed`` could approve its own actions, so Apps use their own domains.

        A task that runs again states each fact once: the event it published earlier is returned.
        """
        if type.split(".", 1)[0] in RESERVED_DOMAINS:
            raise ReservedEventError(f"agents can't publish {type}: the platform owns that domain")
        return await self._publish_once(type, data)

    async def _publish_once(self, type: str, data: Any) -> Event:
        site = self.sites.next("emit")
        key = site.key("emit")
        # In a replay or fork, what an emit would set off mustn't run.
        held = holding_back(self.event) is not None
        recorded = await self._emitted("agent.emit.stubbed" if held else type, key, site)
        if recorded is not None and not held:
            return recorded
        payload: JsonValue = (
            cast(JsonValue, data.model_dump(mode="json"))
            if isinstance(data, BaseModel)
            else cast(JsonValue, data)
        )
        if held:
            if recorded is None:
                await self._bus.publish(
                    self.event.derive(
                        "agent.emit.stubbed",
                        EmitStubbed(type=type, data=payload).model_dump(mode="json"),
                        source=self.source,
                        principal=self.principal,
                        dataclass=self.data_class,
                        tainted=self.tainted,
                        idempotencykey=key,
                    )
                )
            return self.event.derive(
                type, payload, source=self.source, principal=self.principal, idempotencykey=key
            )  # what it would have published: the agent goes on as it would
        event = self.event.derive(
            type,
            payload,
            source=self.source,
            principal=self.principal,
            dataclass=self.data_class,
            tainted=self.tainted,
            idempotencykey=key,
        )
        await self._bus.publish(event)
        return event

    async def _emitted(self, type: str, key: str, site: CallSite) -> Event | None:
        if self.run_id is None:
            return None
        scope = Scope(self.tenant, self.app)
        async for stored in self._bus.replay(scope, type, run_id=self.run_id):
            if stored.event.idempotencykey == key and site.recorded_by(stored.event):
                return stored.event
        return None
