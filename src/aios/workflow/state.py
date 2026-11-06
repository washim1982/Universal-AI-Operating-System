"""Run state as a fold over the run's events (ADR-006).

Nothing about a run lives only in memory: the engine rebuilds this state from the log for
every event it handles, so a crash or a restart loses nothing and recovery needs no special
code. Positions in the log order everything, including loops (a step re-runs when one of its
inputs completed after the step last started).

What a run holds is a fold too. Its data label and its taint are the strictest of everything in
its log, not of the one event that happened to trigger the next step, so neither gets lost where
a fan-out joins, or between a step and the one after it (FR-SEC-09, FR-SEC-11).

A run is ``paused`` while anything is in ``waiting``. Only a requested pause is a **hold**, which
stops all new work: an approval or a signal stops just its own step, or its own fan-out item.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, cast

from aios.core.envelope import DataClass, Event
from aios.fabric.port import StoredEvent

ENGINE_PRINCIPAL = "system:workflow"
HOLD = "request"
StepStatus = Literal["pending", "running", "completed", "looped", "failed"]
RunStatus = Literal["new", "running", "paused", "completed", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class Signal:
    """External input the run holds until a wait step takes it (FR-WF-08)."""

    id: str
    name: str
    data: Any


@dataclass(frozen=True, slots=True)
class Decision:
    """How an approval ended, for the step or task waiting on it."""

    event: Event
    approved: bool
    by: str | None = None


@dataclass
class ItemState:
    """One fan-out item (or the single task of a task step)."""

    index: int | None
    attempt: int = 0
    task_id: str | None = None
    status: Literal["queued", "running", "suspended", "completed", "failed"] = "queued"
    output: Any = None
    error: dict[str, Any] | None = None
    request: Event | None = None
    """The latest ``task.requested``: resuming the task repeats it."""
    approval: str | None = None
    """What the task is suspended for."""


@dataclass
class StepState:
    id: str
    status: StepStatus = "pending"
    runs: int = 0
    """How many times the step started (the loop number of its latest run, plus one)."""
    started_pos: int | None = None
    started_at: datetime | None = None
    started_event: Event | None = None
    held_before: timedelta = timedelta()
    """How long the run had been held when this step started."""
    completed_pos: int | None = None
    output: Any = None
    loops_used: int = 0
    """How many times this step's on_result sent the run back (bounded by maxLoops)."""
    items: dict[int | None, ItemState] = field(default_factory=dict[int | None, ItemState])
    approval: str | None = None
    copied: bool = False
    """Kept from the run this one replays, forks or retries, not run here."""

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
    source: dict[str, Any] | None = None
    """The run this one replays, forks or retries, from its request: known before it starts."""
    steps: dict[str, StepState] = field(default_factory=dict[str, StepState])
    tasks: dict[str, tuple[str, int | None, int]] = field(
        default_factory=dict[str, tuple[str, int | None, int]]
    )
    """task.requested id -> (step, item, attempt)."""
    caused: set[str] = field(default_factory=set[str])
    """Ids of events the engine already reacted to (its output's causation ids)."""
    results: list[Event] = field(default_factory=list[Event])
    """Every task result for a current item, in log order."""
    signals: list[Signal] = field(default_factory=list[Signal])
    """Signals no wait step has taken yet, oldest first."""
    decisions: dict[str, Decision] = field(default_factory=dict[str, Decision])
    """Approvals that have been answered, by id."""
    data_class: DataClass = DataClass.PUBLIC
    """The strictest label of anything in the run."""
    tainted: bool = False
    taint: set[str] = field(default_factory=set[str])
    """Where the untrusted content came from, as far as the log can say."""
    waiting: set[str] = field(default_factory=set[str])
    """Why the run is paused: ``request``, ``approval:<step>`` or ``signal:<step>``."""
    held_since: datetime | None = None
    held_for: timedelta = timedelta()
    """Time spent held, which doesn't count against timeouts or the run's duration."""
    ended: Event | None = None
    tasks_requested: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cancel_requested: bool = False
    last_position: int = 0
    events: int = 0

    @property
    def finished(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")

    @property
    def held(self) -> bool:
        return HOLD in self.waiting

    @property
    def unanswered(self) -> list[Event]:
        """Task results the engine has not acted on.

        This reads the log rather than counting what arrived during a hold: a result can come
        before the pause in the log and still reach the engine after it, in another group.
        """
        return [result for result in self.results if result.id not in self.caused]

    def step(self, step_id: str) -> StepState:
        return self.steps.setdefault(step_id, StepState(step_id))

    def outputs(self) -> dict[str, Any]:
        return {sid: s.output for sid, s in self.steps.items() if s.completed_pos is not None}

    def run_deadline(self, duration: timedelta) -> datetime | None:
        """When the run's time budget ends; time held doesn't count."""
        return None if self.started_at is None else self.started_at + duration + self.held_for

    def step_deadline(self, step: StepState, timeout: timedelta) -> datetime | None:
        """When ``step`` times out; time held since it started doesn't count."""
        if step.started_at is None:
            return None
        return step.started_at + timeout + self.held_for - step.held_before

    # ------------------------------------------------------------------ the fold

    def apply(self, stored: StoredEvent) -> None:
        event = stored.event
        position = stored.position
        self.last_position = position
        self.events += 1
        self.data_class = DataClass.strictest(self.data_class, event.dataclass)
        self.tainted = self.tainted or event.tainted
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

    def _on_app_request_received(self, event: Event, data: dict[str, Any], position: int) -> None:
        if event.tainted:
            self.taint.add("input")
        source = data.get("source")
        if isinstance(source, dict):
            self.source = cast(dict[str, Any], source)

    def _on_tool_call_completed(self, event: Event, data: dict[str, Any], position: int) -> None:
        if data.get("untrusted"):
            self.taint.add(f"tool:{data.get('tool')}")

    def _on_memory_recalled(self, event: Event, data: dict[str, Any], position: int) -> None:
        self.taint.update(f"memory:{item}" for item in data.get("items", ()))

    def _on_security_injection_suspected(
        self, event: Event, data: dict[str, Any], position: int
    ) -> None:
        self.taint.add(f"suspected:{data.get('source')}")

    def _on_run_started(self, event: Event, data: dict[str, Any], position: int) -> None:
        self.status = "running"
        self.tenant, self.app = event.tenantid, event.appid
        self.workflow = str(data.get("workflow"))
        self.version = str(data.get("version"))
        self.input = data.get("input")
        self.started_at = event.time
        self.started_event = event

    def _on_run_paused(self, event: Event, data: dict[str, Any], position: int) -> None:
        wait = _wait(data)
        self.waiting.add(wait)
        self.status = "paused"
        if wait == HOLD:
            self.held_since = event.time

    def _on_run_resumed(self, event: Event, data: dict[str, Any], position: int) -> None:
        wait = _wait(data)
        self.waiting.discard(wait)
        self.status = "paused" if self.waiting else "running"
        if wait == HOLD and self.held_since is not None:
            self.held_for += event.time - self.held_since
            self.held_since = None

    def _on_run_completed(self, event: Event, data: dict[str, Any], position: int) -> None:
        self.status, self.ended = "completed", event

    def _on_run_failed(self, event: Event, data: dict[str, Any], position: int) -> None:
        self.status, self.ended = "failed", event

    def _on_run_cancelled(self, event: Event, data: dict[str, Any], position: int) -> None:
        self.status, self.ended = "cancelled", event

    def _on_run_cancel_requested(self, event: Event, data: dict[str, Any], position: int) -> None:
        self.cancel_requested = True

    def _on_run_signal_requested(self, event: Event, data: dict[str, Any], position: int) -> None:
        self.signals.append(Signal(event.id, str(data.get("name")), data.get("data")))

    def _on_step_started(self, event: Event, data: dict[str, Any], position: int) -> None:
        step = self.step(str(data["step"]))
        step.status = "running"
        step.runs += 1
        step.started_pos = position
        step.started_at = event.time
        step.started_event = event
        step.held_before = self.held_for
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
        item.request, item.approval = event, None
        self.tasks[event.id] = (step_id, item_index, attempt)
        if not data.get("resumes"):  # the same task, not more work
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
            self.results.append(event)

    def _on_task_failed(self, event: Event, data: dict[str, Any], position: int) -> None:
        item = self._item_for(event)
        if item is not None and item.status == "running":
            item.status, item.error = "failed", data.get("error")
            self.results.append(event)

    def _on_task_suspended(self, event: Event, data: dict[str, Any], position: int) -> None:
        item = self._item_for(event)
        if item is not None and item.status == "running":
            item.status, item.approval = "suspended", str(data["approval"])
            self.results.append(event)

    def _on_step_retrying(self, event: Event, data: dict[str, Any], position: int) -> None:
        return  # the task.requested that follows records the new attempt

    def _on_step_completed(self, event: Event, data: dict[str, Any], position: int) -> None:
        step = self.step(str(data["step"]))
        step.output = data.get("output")
        step.completed_pos = position
        if data.get("copied_from") is not None:  # kept from a source: done, so it doesn't run
            step.copied = True
            self.taint.update(data.get("taint") or ())  # and it keeps where it came from
            step.started_pos = step.started_pos if step.started_pos is not None else position
            step.runs = max(step.runs, int(data.get("loop", 0)) + 1)
        step.status = "looped" if data.get("looped") else "completed"
        if data.get("looped"):
            step.loops_used += 1
        if data.get("signal") is not None:
            self.signals = [s for s in self.signals if s.id != data["signal"]]

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

    def _on_approval_granted(self, event: Event, data: dict[str, Any], position: int) -> None:
        self.decisions[str(data["approval"])] = Decision(event, True, str(data["by"]))

    def _on_approval_denied(self, event: Event, data: dict[str, Any], position: int) -> None:
        self.decisions[str(data["approval"])] = Decision(event, False, str(data["by"]))

    def _on_approval_expired(self, event: Event, data: dict[str, Any], position: int) -> None:
        self.decisions[str(data["approval"])] = Decision(event, bool(data.get("approved")))


def _wait(data: dict[str, Any]) -> str:
    reason = str(data.get("reason"))
    if reason == HOLD:
        return HOLD
    item = data.get("item")
    return f"{reason}:{data.get('step')}" + (f"#{item}" if item is not None else "")


def fold(run_id: str, events: list[StoredEvent]) -> RunState:
    state = RunState(run_id)
    for stored in events:
        state.apply(stored)
    return state
