"""The Workflow Engine (WP 1.3; FR-WF-01 to 05, 07 to 12; ADR-006).

The engine interprets the graph IR. For every trigger — ``app.request.received``,
``task.completed`` / ``task.failed``, ``run.cancel.requested``, approval decisions, timers — it
rebuilds the run's state from the log, decides what happens next, and publishes the resulting
events as one atomic batch, caused by the trigger. Because the state is a fold and the batch is
atomic, a crash at any point is recovered by redelivery; a trigger whose effects are already in
the log is skipped, so redelivery never duplicates work (M1 exit criterion 2).

Steps run when their inputs are ready: a step (re)runs when one of its ``needs`` completed after
the step last started. That one rule covers sequencing, fan-in and bounded ``onResult`` loops.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import JsonValue

from aios.core.catalog import (
    AppRequestReceived,
    ApprovalRequested,
    BudgetExceeded,
    ErrorInfo,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    StepCompleted,
    StepFailed,
    StepRetrying,
    StepStarted,
    TaskRequested,
    Usage,
)
from aios.core.envelope import Event
from aios.core.errors import BusClosedError
from aios.core.ids import new_ulid, ulid_for, ulid_int
from aios.core.manifests import (
    AppManifest,
    ApprovalStep,
    DynamicStep,
    EventEntrypoint,
    Step,
    TaskStep,
    ToolStep,
    WorkflowManifest,
)
from aios.core.topics import matches
from aios.fabric.port import Delivery, EventBus, Scope, StoredEvent, Subscription
from aios.workflow.expressions import ExpressionError, lookup, resolve
from aios.workflow.state import ENGINE_PRINCIPAL, ItemState, RunState, StepState, fold

logger = logging.getLogger("aios.workflow")

TOOL_CAPABILITY = "aios.tool.run"
"""Capability of the kernel's built-in tool runner, which executes ``type: tool`` steps."""

_TRIGGERS = (
    ("app.request.received", "workflow.requests"),
    ("task.*", "workflow.tasks"),
    ("run.cancel.requested", "workflow.control"),
    ("approval.*", "workflow.approvals"),
)


@dataclass(frozen=True)
class AppDefinition:
    """What the engine needs to know about an installed App."""

    manifest: AppManifest
    workflows: Mapping[str, WorkflowManifest]
    """The latest version of each workflow: new runs use these."""
    capabilities: frozenset[str] = frozenset()
    versions: Mapping[tuple[str, str], WorkflowManifest] = field(
        default_factory=dict[tuple[str, str], WorkflowManifest]
    )
    """Every installed version, so in-flight runs finish on theirs (FR-WF-09, FR-APP-04)."""

    def workflow(self, name: str, version: str | None = None) -> WorkflowManifest | None:
        if version is not None and (name, version) in self.versions:
            return self.versions[(name, version)]
        return self.workflows.get(name)


@dataclass
class WorkflowCatalog:
    apps: dict[tuple[str, str], AppDefinition] = field(
        default_factory=dict[tuple[str, str], AppDefinition]
    )

    def install(self, tenant: str, definition: AppDefinition) -> None:
        self.apps[(tenant, definition.manifest.metadata.name)] = definition

    def get(self, tenant: str, app: str) -> AppDefinition | None:
        return self.apps.get((tenant, app))


class _Failure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _Planner:
    """Builds the batch for one trigger, applying each event to the state as it goes."""

    def __init__(
        self,
        state: RunState,
        trigger: Event,
        app: AppDefinition | None,
        *,
        answered: Event | None = None,
    ) -> None:
        self.state = state
        self.trigger = trigger
        self.app = app
        self.answered = answered
        """The ``task.requested`` a task result answers (its input is reused for retries)."""
        self.out: list[Event] = []
        started = state.started_event
        self.traceparent = trigger.traceparent or (started.traceparent if started else None)
        """Every event of a run carries the run's trace context (FR-OBS-01)."""

    @property
    def workflow(self) -> WorkflowManifest:
        assert self.app is not None  # noqa: S101 - checked when the run started
        workflow = self.app.workflow(self.state.workflow, self.state.version)
        assert workflow is not None  # noqa: S101 - checked when the run started
        return workflow

    def emit(self, type: str, data: Any, *, cause: Event | None = None) -> Event:
        payload: JsonValue = data.model_dump(mode="json") if hasattr(data, "model_dump") else data
        event = (cause or self.trigger).derive(
            type,
            payload,
            source=f"aios://{self.trigger.tenantid}/system/workflow",
            principal=ENGINE_PRINCIPAL,
        )
        if event.traceparent is None and self.traceparent is not None:
            event = event.model_copy(update={"traceparent": self.traceparent})
        self.out.append(event)
        self.state.apply(StoredEvent(self.state.last_position + 1, event))
        return event

    @property
    def usage(self) -> Usage:
        state = self.state
        return Usage(
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            cost_usd=round(state.cost_usd, 8),
        )

    # ------------------------------------------------------------------ run lifecycle

    def start_run(self) -> None:
        request = AppRequestReceived.model_validate(self.trigger.data or {})
        if self.app is None:
            self.fail_run("app.not_installed", f"App {self.trigger.appid!r} is not installed")
            return
        name = request.workflow or next(
            (
                e.workflow
                for e in self.app.manifest.spec.entrypoints
                if e.type == request.entrypoint
            ),
            None,
        )
        workflow = self.app.workflows.get(name or "")
        if workflow is None:
            self.fail_run(
                "workflow.not_found", f"no workflow for entrypoint {request.entrypoint!r}"
            )
            return
        started = self.emit(
            "run.started",
            RunStarted(
                workflow=workflow.metadata.name,
                version=workflow.metadata.version,
                input=request.input,
            ),
        )
        if started.traceparent is None:  # the access layer didn't start a trace: start one
            self.traceparent = _traceparent(started.runid or "")
            started = started.model_copy(update={"traceparent": self.traceparent})
            self.out[-1] = started
            self.state.started_event = started
        self.trigger = started  # the run's first steps are caused by its start
        self.advance()

    def fail_run(self, code: str, message: str) -> None:
        self.emit(
            "run.failed",
            RunFailed(error=ErrorInfo(code=code, message=message), usage=self.usage),
        )

    def cancel(self) -> None:
        reason = (
            (self.trigger.data or {}).get("reason", "")
            if isinstance(self.trigger.data, dict)
            else ""
        )
        self.emit("run.cancelled", RunCancelled(reason=str(reason), usage=self.usage))

    # ------------------------------------------------------------------ scheduling

    def needs(self, step: Step) -> list[str]:
        if step.needs is not None:
            return list(step.needs)
        steps = self.workflow.spec.steps
        index = next(i for i, s in enumerate(steps) if s.id == step.id)
        return [steps[index - 1].id] if index > 0 else []

    def ready(self, step: Step) -> bool:
        state = self.state.steps.get(step.id)
        if state is not None and state.status in ("running", "failed"):
            return False
        completed: list[int] = []
        for need in self.needs(step):
            needed = self.state.steps.get(need)
            if needed is None or needed.status != "completed" or needed.completed_pos is None:
                return False
            completed.append(needed.completed_pos)
        if state is None or state.started_pos is None:
            return True
        return bool(completed) and state.started_pos < max(completed)

    def advance(self) -> None:
        """Start every ready step; complete the run when nothing is left to do."""
        progress = True
        while progress and not self.state.finished:
            progress = False
            for step in self.workflow.spec.steps:
                if self.state.finished:
                    return
                if self.ready(step):
                    self.start_step(step)
                    progress = True
        if self.state.finished:
            return
        steps = [self.state.steps.get(s.id) for s in self.workflow.spec.steps]
        if all(s is not None and s.status == "completed" for s in steps):
            try:
                output = self.run_output()
            except ExpressionError as exc:
                self.fail_run("workflow.invalid_output", str(exc))
                return
            self.emit("run.completed", RunCompleted(output=output, usage=self.usage))

    def run_output(self) -> Any:
        spec = self.workflow.spec
        if spec.output is not None:
            return resolve(spec.output, self.state.input, self.state.outputs())
        return self.state.steps[spec.steps[-1].id].output

    def input_for(self, step: Step, feedback: Any = None) -> Any:
        outputs = self.state.outputs()
        binding = getattr(step, "input", None)
        if binding:
            value = resolve(binding, self.state.input, outputs)
        else:
            needs = self.needs(step)
            if not needs:
                value = self.state.input
            elif len(needs) == 1:
                value = outputs.get(needs[0])
            else:
                value = {need: outputs.get(need) for need in needs}
        if feedback is None:
            return value
        if isinstance(value, dict):
            return {**cast(dict[str, Any], value), "feedback": feedback}
        return {"input": value, "feedback": feedback}

    def capability(self, step: TaskStep | DynamicStep) -> str:
        pattern = step.capability
        known = sorted(self.app.capabilities) if self.app else []
        if "*" not in pattern and ">" not in pattern:
            return pattern
        for capability in known:
            if matches(pattern, capability):
                return capability
        raise _Failure(
            "capability.not_found", f"no agent provides a capability matching {pattern!r}"
        )

    def start_step(self, step: Step, feedback: Any = None) -> None:
        state = self.state.step(step.id)
        loop = state.runs
        try:
            if isinstance(step, DynamicStep):
                source = self.state.steps.get(step.from_plan.split(".", 1)[0])
                items = lookup(step.from_plan, self.state.input, self.state.outputs())
                if source is None or not isinstance(items, list):
                    raise _Failure("workflow.invalid_plan", f"{step.from_plan} is not a list")
                items_list = cast(list[Any], items)
                started = self.emit(
                    "step.started", StepStarted(step=step.id, loop=loop, items=len(items_list))
                )
                if not items_list:
                    self.emit("step.completed", StepCompleted(step=step.id, output=[], loop=loop))
                    return
                capability = self.capability(step)
                for index in range(min(len(items_list), step.max_parallel)):
                    self.request(step, capability, items_list[index], started, item=index)
                return
            started = self.emit("step.started", StepStarted(step=step.id, loop=loop))
            if isinstance(step, ApprovalStep):
                self.emit(
                    "approval.requested",
                    ApprovalRequested(
                        approval=new_ulid(),
                        approvers=list(step.approvers),
                        reason=f"{self.state.workflow} step {step.id}",
                    ),
                    cause=started,
                )
                self.emit("run.paused", {"step": step.id}, cause=started)
                return
            if isinstance(step, ToolStep):
                task_input = {"tool": step.tool, "input": self.input_for(step)}
                self.request(step, TOOL_CAPABILITY, task_input, started)
                return
            self.request(step, self.capability(step), self.input_for(step, feedback), started)
        except (ExpressionError, _Failure) as exc:
            code = exc.code if isinstance(exc, _Failure) else "workflow.invalid_input"
            self.fail_step(step, ErrorInfo(code=code, message=str(exc)))

    def request(
        self,
        step: Step,
        capability: str,
        task_input: Any,
        cause: Event,
        *,
        item: int | None = None,
        attempt: int = 1,
    ) -> None:
        if self.over_budget(extra_tasks=1):
            return
        self.emit(
            "task.requested",
            TaskRequested(
                capability=capability,
                input=task_input,
                step=step.id,
                item=item,
                loop=self.state.step(step.id).loop,
                attempt=attempt,
            ),
            cause=cause,
        )

    def fail_step(self, step: Step, error: ErrorInfo) -> None:
        self.emit("step.failed", StepFailed(step=step.id, error=error))
        self.emit("run.failed", RunFailed(error=error, usage=self.usage))

    def over_budget(self, *, extra_tasks: int = 0) -> bool:
        if self.state.finished:
            return True
        budget = self.workflow.spec.budget
        app_budget = self.app.manifest.spec.budgets.per_run_usd if self.app else None
        limits = [v for v in (budget.max_cost_usd, app_budget) if v is not None]
        exceeded: tuple[str, float, float] | None = None
        if limits and self.state.cost_usd > min(limits):
            exceeded = ("cost", min(limits), round(self.state.cost_usd, 8))
        tasks = self.state.tasks_requested + extra_tasks
        if budget.max_steps is not None and tasks > budget.max_steps:
            exceeded = ("steps", budget.max_steps, tasks)
        if exceeded is None:
            return False
        name, limit, observed = exceeded
        self.emit(
            "budget.exceeded",
            BudgetExceeded.model_validate({"budget": name, "limit": limit, "observed": observed}),
        )
        self.fail_run("budget.exceeded", f"run {name} budget exceeded: {observed:g} > {limit:g}")
        return True

    # ------------------------------------------------------------------ results

    def on_task_result(self) -> None:
        current = self._current_item()
        if current is None or self.over_budget():
            return
        step, state, item, attempt = current
        if item.status == "failed":
            self._retry_or_fail(step, item, attempt)
        elif isinstance(step, DynamicStep):
            self._on_item_done(step, state)
        else:
            self._complete(step, state, item.output)

    def _current_item(self) -> tuple[Step, StepState, ItemState, int] | None:
        """The step item this result answers, unless the result is stale."""
        ref = self.state.tasks.get(self.trigger.causationid or "")
        if ref is None:
            return None
        step_id, index, attempt = ref
        step = next((s for s in self.workflow.spec.steps if s.id == step_id), None)
        state = self.state.steps.get(step_id)
        if step is None or state is None or state.status != "running":
            return None
        item = state.items.get(index)
        if item is None or item.task_id != self.trigger.causationid:
            return None  # superseded by a retry or a timeout
        return step, state, item, attempt

    def _retry_or_fail(self, step: Step, item: ItemState, attempt: int) -> None:
        error = ErrorInfo.model_validate(item.error or {"code": "task.failed", "message": ""})
        if attempt >= step.max_attempts or self.answered is None:
            self.fail_step(step, error)
            return
        self.emit("step.retrying", StepRetrying(step=step.id, attempt=attempt + 1, error=error))
        data = cast(dict[str, Any], self.answered.data)
        self.request(
            step,
            str(data["capability"]),
            data.get("input"),
            self.trigger,
            item=item.index,
            attempt=attempt + 1,
        )

    def _complete(self, step: Step, state: StepState, output: Any) -> None:
        """Complete a task or tool step, following ``onResult`` back-edges within maxLoops."""
        target = None
        max_loops = 0
        if isinstance(step, TaskStep) and step.on_result:
            target = step.on_result.get(_result_value(output) or "")
            max_loops = step.max_loops
        if target is not None and state.loops_used < max_loops:
            self.emit(
                "step.completed",
                StepCompleted(step=step.id, output=output, loop=state.loop, looped=True),
            )
            target_step = next(s for s in self.workflow.spec.steps if s.id == target)
            self.start_step(target_step, feedback=output)
        else:
            exhausted = target is not None
            self.emit(
                "step.completed",
                StepCompleted(
                    step=step.id, output=output, loop=state.loop, loops_exhausted=exhausted
                ),
            )
        self.advance()

    def _on_item_done(self, step: DynamicStep, state: StepState) -> None:
        items = [state.items[i] for i in sorted(i for i in state.items if i is not None)]
        if all(item.status == "completed" for item in items):
            self.emit(
                "step.completed",
                StepCompleted(step=step.id, output=[i.output for i in items], loop=state.loop),
            )
            self.advance()
            return
        running = sum(item.status == "running" for item in items)
        queued = [item for item in items if item.status == "queued"]
        plan = lookup(step.from_plan, self.state.input, self.state.outputs())
        capability = self.capability(step)
        for item in queued[: max(step.max_parallel - running, 0)]:
            assert item.index is not None  # noqa: S101 - fan-out items are numbered
            started = state.started_event or self.trigger
            self.request(
                step, capability, cast(list[Any], plan)[item.index], started, item=item.index
            )

    def on_approval(self) -> None:
        data: dict[str, Any] = self.trigger.data if isinstance(self.trigger.data, dict) else {}
        approval = data.get("approval")
        for step in self.workflow.spec.steps:
            state = self.state.steps.get(step.id)
            if state is None or state.approval != approval or state.status != "running":
                continue
            self.emit("run.resumed", {"step": step.id})
            if self.trigger.type == "approval.granted":
                output = {"approved": True, "by": data.get("by")}
                self.emit(
                    "step.completed", StepCompleted(step=step.id, output=output, loop=state.loop)
                )
                self.advance()
            else:
                code = (
                    "approval.denied"
                    if self.trigger.type == "approval.denied"
                    else "approval.expired"
                )
                self.fail_step(step, ErrorInfo(code=code, message=f"approval {self.trigger.type}"))
            return

    def on_timer(self, now: datetime) -> None:
        workflow = self.workflow
        started_at = self.state.started_at
        duration = workflow.spec.budget.max_duration
        if started_at is not None and duration is not None and now >= started_at + duration:
            cause = self.state.started_event or self.trigger
            elapsed = (now - started_at).total_seconds()
            self.emit(
                "budget.exceeded",
                BudgetExceeded.model_validate(
                    {"budget": "time", "limit": duration.total_seconds(), "observed": elapsed}
                ),
                cause=cause,
            )
            self.emit(
                "run.failed",
                RunFailed(
                    error=ErrorInfo(code="budget.exceeded", message="run time budget exceeded"),
                    usage=self.usage,
                ),
                cause=cause,
            )
            return
        for step in workflow.spec.steps:
            state = self.state.steps.get(step.id)
            if (
                state is None
                or state.status != "running"
                or step.timeout is None
                or state.started_at is None
                or state.started_event is None
            ):
                continue
            if now >= state.started_at + step.timeout:
                error = ErrorInfo(code="step.timeout", message=f"step {step.id} timed out")
                self.emit(
                    "step.failed", StepFailed(step=step.id, error=error), cause=state.started_event
                )
                self.emit(
                    "run.failed",
                    RunFailed(error=error, usage=self.usage),
                    cause=state.started_event,
                )
                return


def _result_value(output: Any) -> str | None:
    """The value ``onResult`` keys on: ``output["result"]``, or the output if it is a string."""
    if isinstance(output, dict):
        value = cast(dict[str, Any], output).get("result")
        return str(value) if value is not None else None
    return output if isinstance(output, str) else None


def _traceparent(run_id: str) -> str:
    """A W3C traceparent whose trace id is the run id (a ULID is 128 bits, like a trace id)."""
    return f"00-{ulid_int(run_id):032x}-{secrets.token_hex(8)}-01"


class WorkflowEngine:
    """Runs workflows for every App in the catalog (one engine leader per installation)."""

    def __init__(
        self,
        bus: EventBus,
        catalog: WorkflowCatalog,
        *,
        scope: Scope | None = None,
        lease: float = 30.0,
    ) -> None:
        self.bus = bus
        self.catalog = catalog
        self.scope = scope or Scope.platform()
        self.lease = lease
        self._subscriptions: list[Subscription] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._locks: dict[str, asyncio.Lock] = {}
        self._timers: list[tuple[datetime, str, str, str]] = []
        self._timer_wake = asyncio.Event()
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for pattern, group in _TRIGGERS:
            subscription = await self.bus.subscribe(
                self.scope, pattern, group=group, ack_wait=self.lease
            )
            self._subscriptions.append(subscription)
            self._tasks.append(asyncio.create_task(self._consume(subscription)))
        await self._recover_timers()
        self._tasks.append(asyncio.create_task(self._timer_loop()))
        for (tenant, _), app in list(self.catalog.apps.items()):
            await self.watch_entrypoints(tenant, app)

    async def watch_entrypoints(self, tenant: str, app: AppDefinition) -> None:
        """Start a run for every fact matching one of the App's event entrypoints (FR-APP-02)."""
        for index, entrypoint in enumerate(app.manifest.spec.entrypoints):
            if not isinstance(entrypoint, EventEntrypoint):
                continue
            subscription = await self.bus.subscribe(
                Scope(tenant, app.manifest.metadata.name),
                entrypoint.on,
                group=f"workflow.entry.{index}",
                ack_wait=self.lease,
            )
            self._subscriptions.append(subscription)
            self._tasks.append(
                asyncio.create_task(self._entrypoint(subscription, entrypoint.workflow, index))
            )

    async def _entrypoint(self, subscription: Subscription, workflow: str, index: int) -> None:
        with contextlib.suppress(BusClosedError):
            async for delivery in subscription:
                event = delivery.event
                if event.type != "app.request.received" and event.principal != ENGINE_PRINCIPAL:
                    await self._start_from(event, workflow, index)
                await delivery.ack()

    async def _start_from(self, event: Event, workflow: str, index: int) -> None:
        # The run id is derived from the event, so a redelivered event can't start a second run.
        run_id = ulid_for(f"{event.id}/{index}", event.time)
        scope = Scope(event.tenantid, event.appid)
        async for _ in self.bus.replay(scope, "app.request.received", run_id=run_id):
            return
        payload = AppRequestReceived(entrypoint="event", workflow=workflow, input=event.data)
        request = event.derive(
            "app.request.received",
            payload.model_dump(mode="json"),
            source=f"aios://{event.tenantid}/apps/{event.appid}/entrypoints/event",
            principal=event.principal,
        )
        await self.bus.publish(
            request.model_copy(update={"runid": run_id, "correlationid": run_id})
        )

    async def stop(self) -> None:
        await self.kill()

    async def kill(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for subscription in self._subscriptions:
            await subscription.close()
        self._subscriptions.clear()
        self._timers.clear()
        self._running = False

    # ------------------------------------------------------------------ triggers

    async def _consume(self, subscription: Subscription) -> None:
        with contextlib.suppress(BusClosedError):
            async for delivery in subscription:
                await self._deliver(delivery)

    async def _deliver(self, delivery: Delivery) -> None:
        event = delivery.event
        own_approval = event.principal == ENGINE_PRINCIPAL and event.type.startswith("approval.")
        if event.runid is None or own_approval:
            await delivery.ack()
            return
        try:
            async with self._lock(event.runid):
                await self._handle(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("engine failed on %s %s; will retry", event.type, event.id)
            await delivery.nack(reason="workflow engine error")
            return
        await delivery.ack()

    def _lock(self, run_id: str) -> asyncio.Lock:
        return self._locks.setdefault(run_id, asyncio.Lock())

    async def load(self, tenant: str, app: str, run_id: str) -> RunState:
        events = [s async for s in self.bus.replay(Scope(tenant, app), run_id=run_id)]
        return fold(run_id, events)

    async def _handle(self, event: Event) -> None:
        if event.type not in (
            "app.request.received",
            "task.completed",
            "task.failed",
            "run.cancel.requested",
            "approval.granted",
            "approval.denied",
            "approval.expired",
        ):
            return
        assert event.runid is not None  # noqa: S101 - checked by _deliver
        state = await self.load(event.tenantid, event.appid, event.runid)
        if event.id in state.caused:
            return  # already handled: this is a redelivery
        app = self.catalog.get(event.tenantid, event.appid)
        planner = _Planner(state, event, app)
        if event.type == "app.request.received":
            if state.status != "new":
                return
            planner.start_run()
        elif state.status == "new" or state.finished or app is None:
            return
        elif event.type == "run.cancel.requested":
            planner.cancel()
        elif event.type.startswith("task."):
            planner.answered = await self._answered(event, state)
            planner.on_task_result()
        else:
            planner.on_approval()
        if planner.out:
            await self.bus.publish(*planner.out)
        self._schedule(planner.state, app)
        if planner.state.finished:
            self._locks.pop(event.runid, None)

    async def _answered(self, event: Event, state: RunState) -> Event | None:
        """The ``task.requested`` event a result answers."""
        scope = Scope(event.tenantid, event.appid)
        async for stored in self.bus.replay(scope, "task.requested.>", run_id=state.run_id):
            if stored.event.id == event.causationid:
                return stored.event
        return None

    # ------------------------------------------------------------------ timers

    def _schedule(self, state: RunState, app: AppDefinition | None) -> None:
        """Durable timers are derived from the log: step timeouts and the run's duration."""
        workflow = app.workflow(state.workflow, state.version) if app is not None else None
        if workflow is None or state.finished:
            return
        due: list[datetime] = []
        if state.started_at is not None and workflow.spec.budget.max_duration is not None:
            due.append(state.started_at + workflow.spec.budget.max_duration)
        for step in workflow.spec.steps:
            step_state = state.steps.get(step.id)
            if (
                step.timeout is not None
                and step_state is not None
                and step_state.status == "running"
                and step_state.started_at is not None
            ):
                due.append(step_state.started_at + step.timeout)
        for when in due:
            heapq.heappush(self._timers, (when, state.run_id, state.tenant, state.app))
        self._timer_wake.set()

    async def _recover_timers(self) -> None:
        async for stored in self.bus.replay(self.scope, "run.started"):
            event = stored.event
            if event.runid is None:
                continue
            state = await self.load(event.tenantid, event.appid, event.runid)
            self._schedule(state, self.catalog.get(event.tenantid, event.appid))

    async def _timer_loop(self) -> None:
        while True:
            self._timer_wake.clear()
            now = datetime.now(UTC)
            while self._timers and self._timers[0][0] <= now:
                _, run_id, tenant, app = heapq.heappop(self._timers)
                await self._fire(run_id, tenant, app, now)
            delay = (self._timers[0][0] - now).total_seconds() if self._timers else 3600.0
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(max(delay, 0.01)):
                    await self._timer_wake.wait()

    async def _fire(self, run_id: str, tenant: str, app_name: str, now: datetime) -> None:
        async with self._lock(run_id):
            state = await self.load(tenant, app_name, run_id)
            app = self.catalog.get(tenant, app_name)
            if state.finished or app is None or state.started_event is None:
                return
            planner = _Planner(state, state.started_event, app)
            planner.on_timer(now)
            if planner.out:
                await self.bus.publish(*planner.out)
