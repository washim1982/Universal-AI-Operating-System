"""The kernel worker: hosts many agents on one event loop (WP 1.2; FR-AGT-03, 04, 05, 08).

* **Dispatch.** Every capability is a consumer group on ``task.requested.<capability>``, so all
  agents providing a capability compete for its tasks and per-run order holds (ADR-005). A
  claimed task publishes ``task.claimed``, runs the handler with a :class:`Context`, and
  publishes ``task.completed`` or ``task.failed`` before the delivery is acknowledged.
* **Choreography.** An agent's ``subscriptions`` are one consumer group per agent and pattern.
* **Effectively once.** A redelivered task whose result is already in the log is acknowledged
  without running again, so a crash between publishing the result and the ack repeats nothing.
* **Loops stop.** When an emitted event would pass the hop limit, the kernel publishes
  ``budget.exceeded`` at the triggering event's depth instead (FR-EVT-11).
* **Lifecycle.** ``agent.lifecycle.*`` events on start and stop; a crashed dispatch loop is
  restarted with backoff; a stop drains in-flight work first.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import JsonValue, ValidationError

from aios.core.catalog import AgentLifecycle, BudgetExceeded, ErrorInfo, TaskFailed
from aios.core.envelope import Event
from aios.core.errors import (
    AIOSError,
    BudgetExceededError,
    BusClosedError,
    HopLimitExceededError,
)
from aios.core.ids import new_ulid
from aios.core.ports import MemoryGateway, ModelGateway, NoMemory, ToolGateway
from aios.fabric.port import Delivery, EventBus, Scope, Subscription
from aios.kernel.context import Context
from aios.kernel.definitions import AgentDefinition

logger = logging.getLogger("aios.kernel")

DEFAULT_TIMEOUT_S = 300.0
_Kind = Literal["task", "event"]


@dataclass(frozen=True, slots=True)
class Installation:
    """The agents of one App installed in one tenant."""

    tenant: str
    app: str
    agents: Sequence[AgentDefinition]

    @property
    def scope(self) -> Scope:
        return Scope(self.tenant, self.app)


class Worker:
    def __init__(
        self,
        bus: EventBus,
        *,
        models: ModelGateway,
        tools: ToolGateway,
        memory: MemoryGateway | None = None,
        installations: Sequence[Installation] = (),
        lease: float = 30.0,
    ) -> None:
        self.bus = bus
        self.models = models
        self.tools = tools
        self.memory = memory or NoMemory()
        self.installations = list(installations)
        self.lease = lease
        self.instance = f"worker-{new_ulid().lower()}"
        self._subscriptions: list[Subscription] = []
        self._loops: list[asyncio.Task[None]] = []
        self._inflight: set[asyncio.Task[None]] = set()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for installation in self.installations:
            for definition in installation.agents:
                await self._lifecycle(installation, definition, "registered")
                await self._lifecycle(installation, definition, "starting")
                await self._serve_agent(installation, definition)
                await self._lifecycle(installation, definition, "ready")

    async def stop(self, *, drain_timeout: float = 10.0) -> None:
        """Graceful stop: take no new work, finish in-flight tasks, then leave the groups."""
        if not self._running:
            return
        for installation in self.installations:
            for definition in installation.agents:
                await self._lifecycle(installation, definition, "draining")
        await self._cancel(self._loops)
        if self._inflight:
            await asyncio.wait(set(self._inflight), timeout=drain_timeout)
        await self._cancel(list(self._inflight))
        await self._close_subscriptions()
        for installation in self.installations:
            for definition in installation.agents:
                await self._lifecycle(installation, definition, "stopped")
        self._running = False

    async def kill(self) -> None:
        """Simulate a crash: drop everything without settling deliveries (the kill test)."""
        await self._cancel([*self._loops, *self._inflight])
        await self._close_subscriptions()
        self._running = False

    async def _cancel(self, tasks: Sequence[asyncio.Task[None]]) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._loops = [task for task in self._loops if not task.done()]

    async def _close_subscriptions(self) -> None:
        for subscription in self._subscriptions:
            await subscription.close()
        self._subscriptions.clear()

    async def _lifecycle(
        self, installation: Installation, definition: AgentDefinition, state: str
    ) -> None:
        if self.bus.closed:
            return
        payload = AgentLifecycle(
            agent=definition.name,
            instance=self.instance,
            capabilities=list(definition.capabilities),
        )
        await self.bus.publish(
            Event(
                source=f"aios://{installation.tenant}/system/kernel",
                type=f"agent.lifecycle.{state}",
                tenantid=installation.tenant,
                appid=installation.app,
                principal="system:kernel",
                data=payload.model_dump(mode="json"),
            )
        )

    # ------------------------------------------------------------------ dispatch

    async def _serve_agent(self, installation: Installation, definition: AgentDefinition) -> None:
        concurrency = asyncio.Semaphore(definition.limits.concurrency)
        tasks: list[tuple[str, str, _Kind]] = [
            (f"task.requested.{capability}", f"agents.{capability}", "task")
            for capability in definition.capabilities
        ]
        facts: list[tuple[str, str, _Kind]] = [
            (subscribed, f"agents.{definition.name}.on{index}", "event")
            for index, subscribed in enumerate(definition.subscriptions)
        ]
        for pattern, group, kind in [*tasks, *facts]:
            subscription = await self.bus.subscribe(
                installation.scope, pattern, group=group, ack_wait=self.lease
            )
            self._subscriptions.append(subscription)
            self._loops.append(
                asyncio.create_task(
                    self._supervised(
                        f"{definition.name}:{pattern}",
                        lambda s=subscription, k=kind: self._serve(s, definition, concurrency, k),
                    )
                )
            )

    async def _supervised(self, name: str, loop: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """Restart a crashed dispatch loop with capped exponential backoff."""
        backoff = 0.1
        while self._running:
            try:
                await loop()
                return
            except BusClosedError:
                return
            except Exception:
                logger.exception("dispatch loop %s crashed; restarting in %.1fs", name, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    async def _serve(
        self,
        subscription: Subscription,
        definition: AgentDefinition,
        concurrency: asyncio.Semaphore,
        kind: _Kind,
    ) -> None:
        while True:
            await concurrency.acquire()
            try:
                delivery = await subscription.next()
            except BaseException:
                concurrency.release()
                raise
            if delivery is None:
                concurrency.release()
                return
            task = asyncio.create_task(self._handle(delivery, definition, kind))
            self._inflight.add(task)

            def done(finished: asyncio.Task[None]) -> None:
                self._inflight.discard(finished)
                concurrency.release()

            task.add_done_callback(done)

    async def _handle(self, delivery: Delivery, definition: AgentDefinition, kind: _Kind) -> None:
        try:
            if kind == "task":
                await self._run_task(delivery, definition)
            else:
                await self._run_subscription(delivery, definition)
        except asyncio.CancelledError:
            raise  # a crash or kill: leave the delivery unsettled so it is redelivered
        except Exception:
            logger.exception("infrastructure error handling %s; will retry", delivery.event.id)
            with contextlib.suppress(Exception):
                await delivery.nack(reason="kernel error")

    def _context(self, event: Event, definition: AgentDefinition) -> Context:
        return Context(
            event=event,
            definition=definition,
            bus=self.bus,
            models=self.models,
            tools=self.tools,
            memory=self.memory,
            instance=self.instance,
            logger=logger,
        )

    async def _run_task(self, delivery: Delivery, definition: AgentDefinition) -> None:
        event = delivery.event
        if await self._already_settled(event):
            await delivery.ack()
            return
        data: dict[str, Any] = event.data if isinstance(event.data, dict) else {}
        capability = str(data.get("capability"))
        ctx = self._context(event, definition)
        await self._reply(
            event,
            "task.claimed",
            {"capability": capability, "agent": definition.name, "instance": self.instance},
            ctx,
        )
        timeout = definition.limits.timeout
        seconds = timeout.total_seconds() if timeout is not None else DEFAULT_TIMEOUT_S
        try:
            payload = definition.parse_input(data.get("input"))
            async with asyncio.timeout(seconds):
                result = await definition.handler(ctx, payload)
            reply_type = "task.completed"
            reply: dict[str, Any] = {
                "capability": capability,
                "output": definition.dump_output(result),
                "usage": ctx.usage.model_dump(),
            }
        except Exception as exc:  # the agent failed; the workflow decides what happens next
            reply_type = "task.failed"
            error = await self._failure(event, ctx, exc)
            reply = TaskFailed(capability=capability, error=error).model_dump(mode="json")
        await self._reply(event, reply_type, reply, ctx)
        await delivery.ack()

    async def _run_subscription(self, delivery: Delivery, definition: AgentDefinition) -> None:
        event = delivery.event
        ctx = self._context(event, definition)
        try:
            async with asyncio.timeout(DEFAULT_TIMEOUT_S):
                await definition.handler(ctx, event)
        except Exception as exc:
            error = await self._failure(event, ctx, exc)
            logger.warning("agent %s failed on %s: %s", definition.name, event.type, error.message)
        await delivery.ack()

    async def _failure(self, event: Event, ctx: Context, exc: Exception) -> ErrorInfo:
        """Turn an agent exception into an error record, publishing budget events first."""
        if isinstance(exc, HopLimitExceededError):
            await self._budget_exceeded(event, "hops", exc.limit, exc.hopcount)
            return ErrorInfo(code=exc.code, message=str(exc))
        if isinstance(exc, BudgetExceededError):
            await self._budget_exceeded(event, exc.budget, exc.limit, exc.observed)
            return ErrorInfo(code=exc.code, message=str(exc))
        if isinstance(exc, TimeoutError):
            return ErrorInfo(
                code="task.timeout", message="the agent ran out of time", retryable=True
            )
        if isinstance(exc, ValidationError):
            return ErrorInfo(code="task.invalid_input", message=str(exc))
        if isinstance(exc, AIOSError):
            retryable = bool(getattr(exc, "retryable", False))
            return ErrorInfo(code=exc.code, message=str(exc), retryable=retryable)
        logger.warning("agent %s raised %r", ctx.agent, exc)
        return ErrorInfo(code="agent.error", message=f"{type(exc).__name__}: {exc}", retryable=True)

    async def _reply(self, event: Event, type: str, data: JsonValue, ctx: Context) -> None:
        reply = event.derive(type, data, source=ctx.source, principal=ctx.principal)
        try:
            await self.bus.publish(reply)
        except HopLimitExceededError as exc:
            await self._budget_exceeded(event, "hops", exc.limit, exc.hopcount)

    async def _budget_exceeded(
        self, trigger: Event, budget: str, limit: float, observed: float
    ) -> None:
        """Publish ``budget.exceeded`` at the trigger's own depth, so that it always fits."""
        fields = trigger.model_dump(
            exclude={"id", "time", "type", "data", "source", "principal", "idempotencykey"}
        )
        payload = BudgetExceeded.model_validate(
            {"budget": budget, "limit": limit, "observed": observed}
        )
        notice = Event.model_validate(
            {
                **fields,
                "type": "budget.exceeded",
                "source": f"aios://{trigger.tenantid}/system/kernel",
                "principal": "system:kernel",
                "causationid": trigger.id,
                "data": payload.model_dump(mode="json"),
            }
        )
        await self.bus.publish(notice)

    async def _already_settled(self, event: Event) -> bool:
        """Whether this task's result is already in the log (it was redelivered after a crash)."""
        if event.runid is None:
            return False
        scope = Scope(event.tenantid, event.appid)
        async for stored in self.bus.replay(scope, "task.>", run_id=event.runid):
            settled = stored.event
            if settled.causationid == event.id and settled.type in (
                "task.completed",
                "task.failed",
            ):
                return True
        return False
