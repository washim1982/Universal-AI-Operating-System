"""A run as the Trace Explorer draws it: its steps as a graph, and what each did (FR-OBS-03).

Every event belongs to the step it was caused by, however far down the causal chain: a model call
made in a task, a policy decision about a tool call in that task, the approval it asked for and
the answer. The graph's edges are the workflow's own, from the version the run started on, so a
run that started before an upgrade is drawn as it really ran.

Inputs and outputs of anything labelled ``restricted`` are left out. A restricted value is kept to
approved models (FR-SEC-11), and a trace is read by more people than that.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, Field, JsonValue

from aios.core.envelope import DataClass, Event
from aios.core.manifests import TaskStep, WorkflowManifest

StepStatus = Literal["pending", "running", "waiting", "completed", "failed"]
REDACTED: dict[str, JsonValue] = {"redacted": "restricted"}


class ModelCall(BaseModel):
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    replayed: bool = False
    at: datetime


class ToolCall(BaseModel):
    tool: str
    status: Literal["requested", "completed", "failed"] = "requested"
    latency_ms: float | None = None
    arguments: JsonValue = None
    error: str | None = None
    stubbed: bool = False
    replayed: bool = False
    at: datetime


class Decision(BaseModel):
    effect: Literal["allowed", "denied", "escalated"]
    action: str
    resource: str
    reason: str = ""
    policies: list[str] = Field(default_factory=list[str])
    at: datetime


class MemoryRead(BaseModel):
    operation: str
    scopes: list[str]
    items: int
    at: datetime


class ApprovalTrace(BaseModel):
    approval: str
    status: Literal["requested", "granted", "denied", "expired", "cancelled"] = "requested"
    summary: str = ""
    risk: str | None = None
    by: str | None = None
    comment: str | None = None
    at: datetime


class TraceNode(BaseModel):
    id: str
    type: str
    needs: list[str] = Field(default_factory=list[str])
    status: StepStatus = "pending"
    attempts: int = 0
    capability: str | None = None
    tool: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: float | None = None
    input: JsonValue = None
    output: JsonValue = None
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    tainted: bool = False
    copied: bool = Field(default=False, description="Kept from the source run, not run here")
    model_calls: list[ModelCall] = Field(default_factory=list[ModelCall])
    tool_calls: list[ToolCall] = Field(default_factory=list[ToolCall])
    decisions: list[Decision] = Field(default_factory=list[Decision])
    memory: list[MemoryRead] = Field(default_factory=list[MemoryRead])
    approvals: list[ApprovalTrace] = Field(default_factory=list[ApprovalTrace])


class TraceEdge(BaseModel):
    source: str
    target: str
    loop: bool = Field(default=False, description="An on_result edge back to an earlier step")


class Moment(BaseModel):
    id: str
    type: str
    at: datetime
    offset_ms: float
    step: str | None = None


class Trace(BaseModel):
    run_id: str
    app: str
    workflow: str | None = None
    version: str | None = None
    status: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    tainted: bool = False
    source: dict[str, JsonValue] | None = Field(
        default=None, description="The run this one replays, forks or retries"
    )
    nodes: list[TraceNode] = Field(default_factory=list[TraceNode])
    edges: list[TraceEdge] = Field(default_factory=list[TraceEdge])
    timeline: list[Moment] = Field(default_factory=list[Moment])


_ENDED = {"run.completed": "completed", "run.failed": "failed", "run.cancelled": "cancelled"}
_DECIDED = {
    "policy.decision.allowed": "allowed",
    "policy.decision.denied": "denied",
    "policy.decision.escalated": "escalated",
}
_ANSWERED = {
    "approval.granted": "granted",
    "approval.denied": "denied",
    "approval.expired": "expired",
    "approval.cancelled": "cancelled",
}


def source_of(found: object, copied: Sequence[str]) -> dict[str, JsonValue] | None:
    """What a replay, fork or retry comes from, in brief, with the steps it kept: their outputs
    are on the steps themselves."""
    if not isinstance(found, dict):
        return None
    data = cast(dict[str, Any], found)
    return {
        "run": data.get("run"),
        "mode": data.get("mode"),
        "step": data.get("step"),
        "model": data.get("model"),
        "live": bool(data.get("live")),
        "copied": list(copied),
    }


def _data(event: Event) -> dict[str, Any]:
    return event.data if isinstance(event.data, dict) else {}


def _shown(value: Any, event: Event) -> JsonValue:
    return REDACTED if event.dataclass is DataClass.RESTRICTED and value is not None else value


def _error(data: dict[str, Any], *, with_code: bool = False) -> str | None:
    found = data.get("error")
    if not isinstance(found, dict):
        return None
    error = cast(dict[str, Any], found)
    message = str(error.get("message", ""))
    return f"{error.get('code')}: {message}" if with_code else message


def _ms(start: datetime | None, end: datetime | None) -> float | None:
    return None if start is None or end is None else (end - start).total_seconds() * 1000


def _steps_of(events: Sequence[Event]) -> dict[str, str | None]:
    """Each event's step: its own, or that of the nearest event up its causal chain that has one."""
    by_id = {event.id: event for event in events}
    found: dict[str, str | None] = {}

    def step_of(event: Event) -> str | None:
        seen: list[str] = []
        current: Event | None = event
        step: str | None = None
        while current is not None and current.id not in found:
            seen.append(current.id)
            own = _data(current).get("step")
            if isinstance(own, str):
                step = own
                break
            current = by_id.get(current.causationid or "")
        else:
            step = found.get(current.id) if current is not None else None
        for event_id in seen:
            found[event_id] = step
        return step

    for event in events:
        step_of(event)
    return found


def _graph(workflow: WorkflowManifest) -> tuple[list[TraceNode], list[TraceEdge]]:
    nodes: list[TraceNode] = []
    edges: list[TraceEdge] = []
    steps = workflow.spec.steps
    for index, step in enumerate(steps):
        needs = (
            list(step.needs) if step.needs is not None else ([steps[index - 1].id] if index else [])
        )
        node = TraceNode(id=step.id, type=step.type, needs=needs)
        node.capability = getattr(step, "capability", None)
        node.tool = getattr(step, "tool", None)
        nodes.append(node)
        edges += [TraceEdge(source=need, target=step.id) for need in needs]
        if isinstance(step, TaskStep):
            edges += [
                TraceEdge(source=step.id, target=target, loop=True)
                for target in dict.fromkeys(step.on_result.values())
            ]
    return nodes, edges


def _apply(node: TraceNode, event: Event) -> None:  # noqa: PLR0912 - one branch per event type
    data, kind = _data(event), event.type
    node.tainted = node.tainted or event.tainted
    if kind == "step.started":
        node.status = "running"
        node.attempts = max(node.attempts, int(data.get("attempt", 1)))
        node.started_at = node.started_at or event.time
    elif kind == "step.completed":
        node.status = "completed"
        node.ended_at = event.time
        node.output = _shown(data.get("output"), event)
        node.copied = data.get("copied_from") is not None
    elif kind == "step.failed":
        node.status = "failed"
        node.ended_at = event.time
        node.error = _error(data, with_code=True)
    elif kind == "task.requested" and node.input is None:
        node.input = _shown(data.get("input"), event)
    elif kind == "model.call.completed":
        usage: dict[str, Any] = data.get("usage") or {}
        node.model_calls.append(
            ModelCall(
                provider=str(data.get("provider")),
                model=str(data.get("model")),
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                cached_tokens=int(usage.get("cache_read_tokens", 0)),
                cost_usd=float(usage.get("cost_usd", 0.0)),
                latency_ms=float(data.get("latency_ms", 0.0)),
                replayed=bool(data.get("replayed")),
                at=event.time,
            )
        )
    elif kind == "tool.call.requested":
        node.tool_calls.append(
            ToolCall(
                tool=str(data.get("tool")),
                arguments=_shown(data.get("arguments"), event),
                at=event.time,
            )
        )
    elif kind in ("tool.call.completed", "tool.call.failed"):
        call = next(
            (
                c
                for c in reversed(node.tool_calls)
                if c.tool == data.get("tool") and c.status == "requested"
            ),
            None,
        )
        if call is not None:
            call.status = "completed" if kind == "tool.call.completed" else "failed"
            call.latency_ms = data.get("latency_ms")
            call.error = _error(data)
            call.stubbed = bool(data.get("stubbed"))
            call.replayed = bool(data.get("replayed"))
    elif kind in _DECIDED:
        node.decisions.append(
            Decision(
                effect=_DECIDED[kind],  # type: ignore[arg-type]
                action=str(data.get("action")),
                resource=str(data.get("resource")),
                reason=str(data.get("reason", "")),
                policies=list(data.get("policies") or []),
                at=event.time,
            )
        )
    elif kind == "memory.recalled":
        node.memory.append(
            MemoryRead(
                operation=str(data.get("operation")),
                scopes=list(data.get("scopes") or []),
                items=len(data.get("items") or []),
                at=event.time,
            )
        )
    elif kind == "approval.requested":
        node.status = "waiting"
        node.approvals.append(
            ApprovalTrace(
                approval=str(data.get("approval")),
                summary=str(data.get("summary", "")),
                risk=data.get("risk"),
                at=event.time,
            )
        )
    elif kind in _ANSWERED:
        approval = next((a for a in node.approvals if a.approval == data.get("approval")), None)
        if approval is not None:
            approval.status = _ANSWERED[kind]  # type: ignore[assignment]
            approval.by = data.get("by")
            approval.comment = data.get("comment")
        if node.status == "waiting":
            node.status = "running"


def trace_of(events: Sequence[Event], workflow: WorkflowManifest | None, *, app: str) -> Trace:
    """The trace of one run. ``events`` are the run's own, in the order they were stored."""
    run_id = next((event.runid for event in events if event.runid), "")
    started = next((event for event in events if event.type == "run.started"), None)
    ended = next((event for event in reversed(events) if event.type in _ENDED), None)
    first = events[0].time if events else None
    nodes, edges = _graph(workflow) if workflow is not None else ([], [])
    by_id = {node.id: node for node in nodes}
    steps = _steps_of(events)
    for event in events:
        step = steps.get(event.id)
        if step is None:
            continue
        if step not in by_id:  # a step this version's graph doesn't have: show it anyway
            by_id[step] = TraceNode(id=step, type="step")
            nodes.append(by_id[step])
        _apply(by_id[step], event)
    for node in nodes:
        node.input_tokens = sum(call.input_tokens for call in node.model_calls)
        node.output_tokens = sum(call.output_tokens for call in node.model_calls)
        node.cost_usd = round(sum(call.cost_usd for call in node.model_calls), 8)
        node.duration_ms = _ms(node.started_at, node.ended_at)
    start_data = _data(started) if started is not None else {}
    status = _ENDED[ended.type] if ended is not None else ("running" if started else "pending")
    return Trace(
        run_id=run_id or "",
        app=app,
        workflow=start_data.get("workflow"),
        version=start_data.get("version"),
        status=status,
        started_at=started.time if started is not None else None,
        ended_at=ended.time if ended is not None else None,
        duration_ms=_ms(started.time if started else None, ended.time if ended else None),
        input_tokens=sum(node.input_tokens for node in nodes),
        output_tokens=sum(node.output_tokens for node in nodes),
        cost_usd=round(sum(node.cost_usd for node in nodes), 8),
        tainted=any(event.tainted for event in events),
        source=source_of(start_data.get("source"), [node.id for node in nodes if node.copied]),
        nodes=nodes,
        edges=edges,
        timeline=[
            Moment(
                id=event.id,
                type=event.type,
                at=event.time,
                offset_ms=_ms(first, event.time) or 0.0,
                step=steps.get(event.id),
            )
            for event in events
        ],
    )
