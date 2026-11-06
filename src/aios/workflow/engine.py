"""The Workflow Engine (WP 1.3; FR-WF-01 to 05, 07 to 12; ADR-006).

The engine interprets the graph IR. For every trigger — ``app.request.received``,
``task.completed`` / ``task.failed``, ``run.*.requested`` commands, approval decisions, timers —
it rebuilds the run's state from the log, decides what happens next, and publishes the resulting
events as one atomic batch, caused by the trigger. Because the state is a fold and the batch is
atomic, a crash at any point is recovered by redelivery; a trigger whose effects are already in
the log is skipped, so redelivery never duplicates work (M1 exit criterion 2).

Steps run when their inputs are ready: a step (re)runs when one of its ``needs`` completed after
the step last started. That one rule covers sequencing, fan-in and bounded ``onResult`` loops.

A requested pause **holds** the run: task results stay in the log unanswered and no new work
starts, so a retry, a loop or the next fan-out item waits as well. Resuming answers those results
in log order, exactly as if they had just arrived. Tasks already requested still finish.

A task that stops for an approval is requested again once the approval is answered, whatever the
answer: the task is what finds out, at the line that asked, and it decides what happens next.
An answer is final, so a failure that comes from one is not retried. Asking again until someone
says yes would make a denial worth nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import logging
import secrets
from collections.abc import Mapping, Sequence
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
    RunPaused,
    RunResumed,
    RunSource,
    RunStarted,
    StepCompleted,
    StepFailed,
    StepRetrying,
    StepStarted,
    TaskRequested,
    TaskSource,
    Usage,
)
from aios.core.envelope import DataClass, Event
from aios.core.errors import BusClosedError
from aios.core.human import DEFAULT_APPROVAL_TIMEOUT
from aios.core.ids import new_ulid, ulid_for, ulid_int
from aios.core.manifests import (
    AppManifest,
    ApprovalStep,
    DynamicStep,
    EventEntrypoint,
    Step,
    TaskStep,
    ToolStep,
    WaitStep,
    WorkflowManifest,
)
from aios.core.topics import matches
from aios.fabric.port import Delivery, EventBus, Scope, StoredEvent, Subscription
from aios.workflow.expressions import ExpressionError, lookup, resolve
from aios.workflow.state import (
    ENGINE_PRINCIPAL,
    Decision,
    ItemState,
    RunState,
    StepState,
    fold,
)

logger = logging.getLogger("aios.workflow")

TOOL_CAPABILITY = "aios.tool.run"
"""Capability of the kernel's built-in tool runner, which executes ``type: tool`` steps."""

_TRIGGERS = (
    ("app.request.received", "workflow.requests"),
    ("task.*", "workflow.tasks"),
    ("run.*.requested", "workflow.run-control"),  # one group, so a run's commands stay in order
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
    data_classes: frozenset[DataClass] | None = None
    """What a tenant admin agreed the App may handle. ``None`` if nobody was asked."""

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

    def remove(self, tenant: str, app: str) -> None:
        self.apps.pop((tenant, app), None)


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
            dataclass=self.state.data_class,
            tainted=self.state.tainted,
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
        workflow = (
            self.app.workflow(name or "", request.version)
            if request.version is not None
            else self.app.workflows.get(name or "")
        )
        if workflow is None or (
            request.version is not None and workflow.metadata.version != request.version
        ):
            self.fail_run(
                "workflow.not_found",
                f"no workflow {name!r} {request.version or ''} for entrypoint"
                f" {request.entrypoint!r}",
            )
            return
        source = request.source
        started = self.emit(
            "run.started",
            RunStarted(
                workflow=workflow.metadata.name,
                version=workflow.metadata.version,
                input=request.input,
                source=source.model_copy(update={"copied": []}) if source else None,  # answers kept
            ),
        )
        if started.traceparent is None:  # the access layer didn't start a trace: start one
            self.traceparent = _traceparent(started.runid or "")
            started = started.model_copy(update={"traceparent": self.traceparent})
            self.out[-1] = started
            self.state.started_event = started
        self.trigger = started  # the run's first steps are caused by its start
        if source is not None and not self.copy_steps(source, started):
            return
        self.advance()

    def copy_steps(self, source: RunSource, started: Event) -> bool:
        """Keep the source's results for the steps a fork or retry doesn't run again."""
        known = {step.id for step in self.workflow.spec.steps}
        for copied in source.copied:
            if copied.step not in known:
                self.fail_run("workflow.not_found", f"no step {copied.step!r} to copy")
                return False
            self.emit(
                "step.completed",
                StepCompleted(
                    step=copied.step,
                    output=copied.output,
                    loop=copied.loop,
                    copied_from=source.run,
                    taint=source.taint,
                ),
                cause=started,
            )
        return True

    def answered_before(self, step: Step, started: Event) -> bool:
        """In a replay or fork, nobody is asked again: the source's answer is given instead."""
        source = self.run_source()
        found = next((a for a in source.answers if a.step == step.id), None) if source else None
        if source is None or found is None:
            return False
        self.emit(
            "step.completed",
            StepCompleted(
                step=step.id, output=found.output, loop=found.loop, copied_from=source.run
            ),
            cause=started,
        )
        return True

    def run_source(self) -> RunSource | None:
        started = self.state.started_event
        data = started.data if started is not None and isinstance(started.data, dict) else {}
        found = data.get("source")
        return RunSource.model_validate(found) if isinstance(found, dict) else None

    def task_source(self) -> TaskSource | None:
        started = self.state.started_event
        data = started.data if started is not None and isinstance(started.data, dict) else {}
        found = data.get("source")
        if not isinstance(found, dict):
            return None
        source = RunSource.model_validate(found)
        return TaskSource(run=source.run, mode=source.mode, model=source.model, live=source.live)

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

    def pause(self) -> None:
        if not self.state.held:
            self.emit("run.paused", RunPaused(reason="request"))

    def resume(self, results: Sequence[tuple[Event, Event | None]]) -> None:
        """Release a hold and answer the results that waited, with what each one answers."""
        if not self.state.held:
            return
        self.emit("run.resumed", RunResumed(reason="request"))
        command = self.trigger
        for result, answered in results:
            self.trigger, self.answered = result, answered
            self.on_task_result()
        self.trigger = command
        self.deliver_decisions()
        self.on_signal()

    def on_signal(self) -> None:
        self.deliver_signals()
        self.advance()

    def deliver_signals(self) -> None:
        """Each waiting step takes the oldest signal of its name, sent at any time (FR-WF-08)."""
        for step in self.workflow.spec.steps:
            state = self.state.steps.get(step.id)
            if (
                self.state.held
                or self.state.finished
                or not isinstance(step, WaitStep)
                or state is None
                or state.status != "running"
            ):
                continue
            signal = next((s for s in self.state.signals if s.name == step.signal), None)
            if signal is not None:
                self.emit("run.resumed", RunResumed(reason="signal", step=step.id))
                self.emit(
                    "step.completed",
                    StepCompleted(
                        step=step.id, output=signal.data, loop=state.loop, signal=signal.id
                    ),
                )

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
        while progress and not self.state.finished and not self.state.held:
            progress = False
            for step in self.workflow.spec.steps:
                if self.state.finished:
                    return
                if self.ready(step):
                    self.start_step(step)
                    progress = True
        if self.state.finished or self.state.held:
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
        changed = self.changed_input(step)
        if changed is not None:
            value = changed
        elif binding:
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

    def changed_input(self, step: Step) -> Any:
        """A fork's new input for the step it starts from, if it was given one."""
        started = self.state.started_event
        data = started.data if started is not None and isinstance(started.data, dict) else {}
        source = data.get("source")
        if not isinstance(source, dict) or source.get("step") != step.id:
            return None
        return cast(dict[str, Any], source).get("step_input")

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
            if isinstance(step, (ApprovalStep, WaitStep)) and self.answered_before(step, started):
                return
            if isinstance(step, ApprovalStep):
                self.emit(
                    "approval.requested",
                    ApprovalRequested(
                        approval=new_ulid(),
                        tainted=self.state.tainted,
                        taint=sorted(self.state.taint),
                        approvers=list(step.approvers),
                        reason=f"{self.state.workflow} step {step.id}",
                        summary=step.summary or f"Continue {self.state.workflow} past {step.id}",
                        payload=self.input_for(step),
                        risk=step.risk,
                        step=step.id,
                        expires_at=started.time + (step.timeout or DEFAULT_APPROVAL_TIMEOUT),
                        on_timeout=step.on_timeout,
                    ),
                    cause=started,
                )
                self.emit("run.paused", RunPaused(reason="approval", step=step.id), cause=started)
                return
            if isinstance(step, WaitStep):
                self.emit("run.paused", RunPaused(reason="signal", step=step.id), cause=started)
                self.deliver_signals()
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
                taint=sorted(self.state.taint),
                source=self.task_source(),
            ),
            cause=cause,
        )

    def fail_step(self, step: Step, error: ErrorInfo, *, cause: Event | None = None) -> None:
        self.emit("step.failed", StepFailed(step=step.id, error=error), cause=cause)
        self.emit("run.failed", RunFailed(error=error, usage=self.usage), cause=cause)

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
        if self.state.held:
            return  # the result stays in the log until resume() answers it
        current = self._current_item()
        if current is None or self.over_budget():
            return
        step, state, item, attempt = current
        if item.status == "suspended":
            self.emit("run.paused", RunPaused(reason="approval", step=step.id, item=item.index))
            self.deliver_decisions()
        elif item.status == "failed":
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
        final = error.code.startswith(("approval.", "budget.", "policy.", "replay.diverged"))
        if final or attempt >= step.max_attempts or self.answered is None:
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
        self.deliver_decisions()
        self.advance()

    def deliver_decisions(self) -> None:
        """Answer each approval step, and each suspended task whose approval is decided.

        This scans rather than reacts to one event, because an answer can reach the engine before
        the task's ``task.suspended``: they are separate consumer groups. A hold stops a suspended
        task from resuming, as it stops all new work, and the resume delivers it.
        """
        for step in self.workflow.spec.steps:
            state = self.state.steps.get(step.id)
            if self.state.finished or state is None or state.status != "running":
                continue
            if isinstance(step, ApprovalStep):
                decision = self.state.decisions.get(state.approval or "")
                if decision is not None:
                    self._finish_approval(step, state, decision)
            elif not self.state.held:
                for item in state.items.values():
                    decision = self.state.decisions.get(item.approval or "")
                    if decision is not None:  # only a suspended item has an approval
                        self._resume_task(step, item, decision)

    def _finish_approval(self, step: ApprovalStep, state: StepState, decision: Decision) -> None:
        cause = decision.event
        self.emit("run.resumed", RunResumed(reason="approval", step=step.id), cause=cause)
        if decision.approved:
            output: JsonValue = {"approved": True, "by": decision.by}
            self.emit(
                "step.completed",
                StepCompleted(step=step.id, output=output, loop=state.loop),
                cause=cause,
            )
        else:
            error = ErrorInfo(code=cause.type, message=f"step {step.id}: {cause.type}")
            self.fail_step(step, error, cause=cause)

    def _resume_task(self, step: Step, item: ItemState, decision: Decision) -> None:
        assert item.request is not None  # noqa: S101 - a suspended task was requested
        requested = TaskRequested.model_validate(item.request.data)
        resumed = requested.model_copy(
            update={
                "resumes": [*requested.resumes, item.request.id],
                "taint": sorted(self.state.taint),
            }
        )
        cause = decision.event
        self.emit(
            "run.resumed", RunResumed(reason="approval", step=step.id, item=item.index), cause=cause
        )
        self.emit("task.requested", resumed, cause=cause)

    def on_timer(self, now: datetime) -> None:
        if self.state.held:
            return  # no clock runs during a hold; resuming schedules the timers again
        workflow = self.workflow
        started_at = self.state.started_at
        duration = workflow.spec.budget.max_duration
        deadline = self.state.run_deadline(duration) if duration is not None else None
        if started_at is not None and duration is not None and deadline and now >= deadline:
            cause = self.state.started_event or self.trigger
            elapsed = (now - started_at - self.state.held_for).total_seconds()
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
                or isinstance(step, ApprovalStep)  # its timeout is the approval's: Human OS's clock
                or state.started_event is None
            ):
                continue
            deadline = self.state.step_deadline(state, step.timeout)
            if deadline is not None and now >= deadline:
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
        self._watching: dict[tuple[str, str], list[tuple[Subscription, asyncio.Task[None]]]] = {}
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
        name = app.manifest.metadata.name
        await self.unwatch_entrypoints(tenant, name)
        watching: list[tuple[Subscription, asyncio.Task[None]]] = []
        self._watching[(tenant, name)] = watching
        for index, entrypoint in enumerate(app.manifest.spec.entrypoints):
            if not isinstance(entrypoint, EventEntrypoint):
                continue
            subscription = await self.bus.subscribe(
                Scope(tenant, app.manifest.metadata.name),
                entrypoint.on,
                group=f"workflow.entry.{index}",
                ack_wait=self.lease,
            )
            task = asyncio.create_task(self._entrypoint(subscription, entrypoint, index))
            watching.append((subscription, task))
            self._subscriptions.append(subscription)
            self._tasks.append(task)

    async def unwatch_entrypoints(self, tenant: str, app: str) -> None:
        """Stop an App's entrypoints, because it is changing or going away."""
        for subscription, task in self._watching.pop((tenant, app), []):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await subscription.close()
            self._subscriptions.remove(subscription)
            self._tasks.remove(task)

    async def _entrypoint(
        self, subscription: Subscription, entrypoint: EventEntrypoint, index: int
    ) -> None:
        with contextlib.suppress(BusClosedError):
            async for delivery in subscription:
                event = delivery.event
                if event.type != "app.request.received" and event.principal != ENGINE_PRINCIPAL:
                    await self._start_from(event, entrypoint, index)
                await delivery.ack()

    async def _start_from(self, event: Event, entrypoint: EventEntrypoint, index: int) -> None:
        app = self.catalog.get(event.tenantid, event.appid)
        allowed = None if app is None else app.data_classes
        if allowed is not None and event.dataclass not in allowed:
            logger.warning("%s wasn't granted %s data", event.appid, event.dataclass.value)
            return
        # The run id is derived from the event, so a redelivered event can't start a second run.
        run_id = ulid_for(f"{event.id}/{index}", event.time)
        scope = Scope(event.tenantid, event.appid)
        async for _ in self.bus.replay(scope, "app.request.received", run_id=run_id):
            return
        payload = AppRequestReceived(
            entrypoint="event", workflow=entrypoint.workflow, input=event.data
        )
        request = event.derive(
            "app.request.received",
            payload.model_dump(mode="json"),
            source=f"aios://{event.tenantid}/apps/{event.appid}/entrypoints/event",
            principal=event.principal,
            tainted=entrypoint.untrusted_input,
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
            "task.suspended",
            "run.cancel.requested",
            "run.pause.requested",
            "run.resume.requested",
            "run.signal.requested",
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
        else:
            await self._react(planner, event, state)
        if planner.out:
            await self.bus.publish(*planner.out)
        self._schedule(planner.state, app)
        if planner.state.finished:
            self._locks.pop(event.runid, None)

    async def _react(self, planner: _Planner, event: Event, state: RunState) -> None:
        if event.type == "run.cancel.requested":
            planner.cancel()
        elif event.type == "run.pause.requested":
            planner.pause()
        elif event.type == "run.resume.requested":
            planner.resume([(r, await self._answered(r, state)) for r in state.unanswered])
        elif event.type == "run.signal.requested":
            planner.on_signal()
        elif event.type.startswith("task."):
            planner.answered = await self._answered(event, state)
            planner.on_task_result()
        else:
            planner.on_approval()

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
        if workflow is None or state.finished or state.held:
            return
        duration = workflow.spec.budget.max_duration
        due = [state.run_deadline(duration)] if duration is not None else []
        for step in workflow.spec.steps:
            step_state = state.steps.get(step.id)
            if (
                step.timeout is not None
                and step_state is not None
                and step_state.status == "running"
            ):
                due.append(state.step_deadline(step_state, step.timeout))
        for when in due:
            if when is not None:
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
