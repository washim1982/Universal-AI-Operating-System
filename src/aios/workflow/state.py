"""Run state as a fold over the run's events (ADR-006).

Nothing about a run lives only in memory: the engine rebuilds this state from the log for
every event it handles, so a crash or a restart loses nothing and recovery needs no special
code. Positions in the log order everything, including loops (a step re-runs when one of its
inputs completed after the step last started).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, cast

from aios.core.envelope import Event
from aios.fabric.port import StoredEvent

ENGINE_PRINCIPAL = "system:workflow"
StepStatus = Literal["pending", "running", "completed", "looped", "failed"]
RunStatus = Literal["new", "running", "paused", "completed", "failed", "cancelled"]


@dataclass
class ItemState:
    """One fan-out item (or the single task of a task step)."""

    index: int | None
    attempt: int = 0
    task_id: str | None = None
    status: Literal["queued", "running", "completed", "failed"] = "queued"
    output: Any = None
    error: dict[str, Any] | None = None


@dataclass
class StepState:
    id: str
    status: StepStatus = "pending"
    runs: int = 0
    """How many times the step started (the loop number of its latest run, plus one)."""
    started_pos: int | None = None
    started_at: datetime | None = None
    started_event: Event | None = None
    completed_pos: int | None = None
    output: Any = None
    loops_used: int = 0
    """How many times this step's on_result sent the run back (bounded by maxLoops)."""
    items: dict[int | None, ItemState] = field(default_factory=dict[int | None, ItemState])
    approval: str | None = None

    @property
    def loop(self) -> int:
        return max(self.runs - 1, 0)


@dataclass
class RunState:
    run_id: str
    status: RunStatus = "new"
    tenant: str = ""
    app: str = ""
    workflow: str = ""
    version: str = ""
    input: Any = None
    started_at: datetime | None = None
    started_event: Event | None = None
    steps: dict[str, StepState] = field(default_factory=dict[str, StepState])
    tasks: dict[str, tuple[str, int | None, int]] = field(
        default_factory=dict[str, tuple[str, int | None, int]]
    )
    """task.requested id -> (step, item, attempt)."""
    caused: set[str] = field(default_factory=set[str])
    """Ids of events the engine already reacted to (its output's causation ids)."""
    tasks_requested: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cancel_requested: bool = False
    last_position: int = 0

    @property
    def finished(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")

    def step(self, step_id: str) -> StepState:
        return self.steps.setdefault(step_id, StepState(step_id))

    def outputs(self) -> dict[str, Any]:
        return {sid: s.output for sid, s in self.steps.items() if s.completed_pos is not None}

    # ------------------------------------------------------------------ the fold

    def apply(self, stored: StoredEvent) -> None:
        event = stored.event
        position = stored.position
        self.last_position = position
        data: dict[str, Any] = event.data if isinstance(event.data, dict) else {}
        if event.principal == ENGINE_PRINCIPAL and event.causationid is not None:
            self.caused.add(event.causationid)
        handler = getattr(self, "_on_" + event.type.replace(".", "_"), None)
        if handler is not None:
            handler(event, data, position)
        elif event.type == "model.call.completed":
            usage = cast(dict[str, Any], data.get("usage") or {})
            self.input_tokens += int(usage.get("input_tokens", 0))
            self.output_tokens += int(usage.get("output_tokens", 0))
            self.cost_usd += float(usage.get("cost_usd", 0.0))

    def _on_run_started(self, event: Event, data: dict[str, Any], position: int) -> None:
        self.status = "running"
        self.tenant, self.app = event.tenantid, event.appid
        self.workflow = str(data.get("workflow"))
        self.version = str(data.get("version"))
        self.input = data.get("input")
        self.started_at = event.time
        self.started_event = event

    def _on_run_paused(self, event: Event, data: dict[str, Any], position: int) -> None:
        self.status = "paused"

    def _on_run_resumed(self, event: Event, data: dict[str, Any], position: int) -> None:
        self.status = "running"

    def _on_run_completed(self, event: Event, data: dict[str, Any], position: int) -> None:
        self.status = "completed"

    def _on_run_failed(self, event: Event, data: dict[str, Any], position: int) -> None:
        self.status = "failed"

    def _on_run_cancelled(self, event: Event, data: dict[str, Any], position: int) -> None:
        self.status = "cancelled"

    def _on_run_cancel_requested(self, event: Event, data: dict[str, Any], position: int) -> None:
        self.cancel_requested = True

    def _on_step_started(self, event: Event, data: dict[str, Any], position: int) -> None:
        step = self.step(str(data["step"]))
        step.status = "running"
        step.runs += 1
        step.started_pos = position
        step.started_at = event.time
        step.started_event = event
        step.items = {}
        step.approval = None
        if data.get("items") is not None:
            step.items = {i: ItemState(i) for i in range(int(data["items"]))}

    def _on_task_requested(self, event: Event, data: dict[str, Any], position: int) -> None:
        step_id = data.get("step")
        if step_id is None or step_id not in self.steps:
            return
        item_index = data.get("item")
        attempt = int(data.get("attempt", 1))
        item = self.steps[step_id].items.setdefault(item_index, ItemState(item_index))
        item.attempt, item.task_id, item.status = attempt, event.id, "running"
        self.tasks[event.id] = (step_id, item_index, attempt)
        self.tasks_requested += 1

    def _item_for(self, event: Event) -> ItemState | None:
        """The current item a task result belongs to, or None if it is stale."""
        ref = self.tasks.get(event.causationid or "")
        if ref is None:
            return None
        step_id, index, attempt = ref
        item = self.steps[step_id].items.get(index)
        if item is None or item.task_id != event.causationid or item.attempt != attempt:
            return None
        return item

    def _on_task_completed(self, event: Event, data: dict[str, Any], position: int) -> None:
        item = self._item_for(event)
        if item is not None and item.status == "running":
            item.status, item.output = "completed", data.get("output")

    def _on_task_failed(self, event: Event, data: dict[str, Any], position: int) -> None:
        item = self._item_for(event)
        if item is not None and item.status == "running":
            item.status, item.error = "failed", data.get("error")

    def _on_step_retrying(self, event: Event, data: dict[str, Any], position: int) -> None:
        return  # the task.requested that follows records the new attempt

    def _on_step_completed(self, event: Event, data: dict[str, Any], position: int) -> None:
        step = self.step(str(data["step"]))
        step.output = data.get("output")
        step.completed_pos = position
        step.status = "looped" if data.get("looped") else "completed"
        if data.get("looped"):
            step.loops_used += 1

    def _on_step_failed(self, event: Event, data: dict[str, Any], position: int) -> None:
        self.step(str(data["step"])).status = "failed"

    def _on_approval_requested(self, event: Event, data: dict[str, Any], position: int) -> None:
        for step in self.steps.values():
            if (
                step.status == "running"
                and step.started_event is not None
                and (event.causationid == step.started_event.id)
            ):
                step.approval = str(data.get("approval"))


def fold(run_id: str, events: list[StoredEvent]) -> RunState:
    state = RunState(run_id)
    for stored in events:
        state.apply(stored)
    return state
