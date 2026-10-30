"""Standard event catalog v0.1 (requirements §7.2, specs/event-catalog.md).

Every platform event type is listed here with its kind, producer and, where it is already
specified, a payload model. The fabric validates payloads against these models on publish in dev
mode (FR-EVT-10), and ``aios specs export`` turns them into JSON Schemas. Events without a payload
model accept any JSON until their work package defines one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from aios.core.envelope import Principal
from aios.core.ids import Ulid
from aios.core.topics import CapabilityName, EventKind, kind_of, validate_topic


class Payload(BaseModel):
    """Base class for catalog payload models."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ErrorInfo(Payload):
    code: str = Field(min_length=1, description="Stable machine-readable code, e.g. tool.timeout.")
    message: str
    retryable: bool = False


class Usage(Payload):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)


class TaskBudget(Payload):
    max_steps: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0)
    timeout_s: float | None = Field(default=None, gt=0)


# --------------------------------------------------------------------------- task


class TaskRequested(Payload):
    capability: CapabilityName
    input: JsonValue = None
    step: str | None = Field(
        default=None, description="Workflow step id, when the task belongs to a run."
    )
    budget: TaskBudget | None = None


class TaskClaimed(Payload):
    capability: CapabilityName
    agent: str = Field(min_length=1)
    instance: str = Field(min_length=1)


class TaskCompleted(Payload):
    capability: CapabilityName
    output: JsonValue = None
    usage: Usage | None = None


class TaskFailed(Payload):
    capability: CapabilityName
    error: ErrorInfo


# --------------------------------------------------------------------------- run


class RunStarted(Payload):
    workflow: str = Field(min_length=1)
    version: str = Field(min_length=1)
    input: JsonValue = None


class RunCompleted(Payload):
    output: JsonValue = None
    usage: Usage | None = None


class RunFailed(Payload):
    error: ErrorInfo


# --------------------------------------------------------------------------- approval


class ApprovalRequested(Payload):
    approval: Ulid
    approvers: list[str] = Field(min_length=1, description="Principals or role:<name> selectors.")
    reason: str
    expires_at: AwareDatetime | None = None


class ApprovalDecided(Payload):
    approval: Ulid
    by: Principal
    comment: str | None = None


# --------------------------------------------------------------------------- budget, system


class BudgetExceeded(Payload):
    budget: Literal["hops", "steps", "tokens", "cost", "time", "events"]
    limit: float
    observed: float


class DeadLetterAdded(Payload):
    dead_letter: Ulid
    group: str
    event: Ulid = Field(description="Id of the dead-lettered event.")
    event_type: str
    attempts: int = Field(ge=1)
    reason: str
    dead_at: AwareDatetime


# --------------------------------------------------------------------------- catalog

PayloadModel = type[Payload]


@dataclass(frozen=True, slots=True)
class EventSpec:
    """One entry in the event catalog."""

    type: str
    emitted_by: str
    description: str
    payload: PayloadModel | None = None
    version: int = 1
    route_by: str | None = None
    """Payload field appended to the physical subject, e.g. ``capability`` for task.requested."""
    kind: EventKind = field(init=False)

    def __post_init__(self) -> None:
        validate_topic(self.type)
        object.__setattr__(self, "kind", kind_of(self.type))

    @property
    def domain(self) -> str:
        return self.type.split(".", 1)[0]

    @property
    def schema_id(self) -> str:
        return f"aios://schemas/{self.type}/{self.version}"


def _spec(
    event_type: str,
    emitted_by: str,
    description: str,
    payload: PayloadModel | None = None,
    *,
    route_by: str | None = None,
) -> EventSpec:
    return EventSpec(event_type, emitted_by, description, payload, route_by=route_by)


_ACCESS = "Access layer"
_CONTROL = "Control plane"
_WORKFLOW = "Workflow Engine"
_KERNEL = "Agent Kernel"
_AGENTS = "Agents (via the SDK)"
_MODELS = "Model Kernel"
_TOOLS = "Tool OS"
_MEMORY = "Memory OS"
_HUMAN = "Human OS"
_SECURITY = "Security Kernel"
_FABRIC = "Event Fabric"

STANDARD_EVENTS: tuple[EventSpec, ...] = (
    _spec("app.request.received", _ACCESS, "A user or trigger asked an App for something."),
    _spec("app.response.ready", _ACCESS, "The App's response is ready to stream to the caller."),
    _spec("app.installed", _CONTROL, "An App was installed into a tenant."),
    _spec("app.upgraded", _CONTROL, "An installed App moved to a new version."),
    _spec("app.uninstalled", _CONTROL, "An App was removed from a tenant."),
    _spec("run.started", _WORKFLOW, "A workflow run started.", RunStarted),
    _spec("run.paused", _WORKFLOW, "A run is waiting (approval, timer, signal)."),
    _spec("run.resumed", _WORKFLOW, "A paused run continued."),
    _spec("run.completed", _WORKFLOW, "A run finished successfully.", RunCompleted),
    _spec("run.failed", _WORKFLOW, "A run failed.", RunFailed),
    _spec("run.cancelled", _WORKFLOW, "A run was cancelled."),
    _spec("step.scheduled", _WORKFLOW, "A step is ready to run."),
    _spec("step.started", _WORKFLOW, "A step started."),
    _spec("step.completed", _WORKFLOW, "A step finished; its output is recorded."),
    _spec("step.failed", _WORKFLOW, "A step failed."),
    _spec("step.retrying", _WORKFLOW, "A failed step will be retried."),
    _spec(
        "task.requested",
        _WORKFLOW,
        "Work for any agent that provides the capability (FR-EVT-16).",
        TaskRequested,
        route_by="capability",
    ),
    _spec("task.claimed", _KERNEL, "An agent instance took the task.", TaskClaimed),
    _spec("task.completed", _AGENTS, "The task's output.", TaskCompleted),
    _spec("task.failed", _AGENTS, "The task failed.", TaskFailed),
    _spec("agent.lifecycle.registered", _KERNEL, "Agent lifecycle transition (FR-AGT-03)."),
    _spec("agent.lifecycle.starting", _KERNEL, "Agent lifecycle transition."),
    _spec("agent.lifecycle.ready", _KERNEL, "Agent lifecycle transition."),
    _spec("agent.lifecycle.busy", _KERNEL, "Agent lifecycle transition."),
    _spec("agent.lifecycle.suspended", _KERNEL, "Agent lifecycle transition."),
    _spec("agent.lifecycle.draining", _KERNEL, "Agent lifecycle transition."),
    _spec("agent.lifecycle.stopped", _KERNEL, "Agent lifecycle transition."),
    _spec("agent.lifecycle.failed", _KERNEL, "Agent lifecycle transition."),
    _spec("model.call.completed", _MODELS, "Summary of a model call: model, tokens, cost."),
    _spec("model.call.failed", _MODELS, "A model call failed after fallbacks."),
    _spec("tool.call.requested", _TOOLS, "A tool invocation, before the policy decision."),
    _spec("tool.call.completed", _TOOLS, "A tool returned."),
    _spec("tool.call.failed", _TOOLS, "A tool failed or was denied."),
    _spec("memory.item.written", _MEMORY, "A memory item was stored."),
    _spec("memory.recalled", _MEMORY, "A context pack was assembled for an agent."),
    _spec("memory.item.forgotten", _MEMORY, "A memory item was erased (GDPR forget)."),
    _spec(
        "approval.requested",
        f"{_WORKFLOW}, {_SECURITY}",
        "Asks Human OS for a decision; the run pauses until it arrives.",
        ApprovalRequested,
    ),
    _spec("approval.granted", _HUMAN, "The approval was granted.", ApprovalDecided),
    _spec("approval.denied", _HUMAN, "The approval was denied.", ApprovalDecided),
    _spec("approval.modified", _HUMAN, "The approver changed the proposed action."),
    _spec("approval.expired", _HUMAN, "No decision before the timeout."),
    _spec("approval.escalated", _HUMAN, "The approval moved to the next approver tier."),
    _spec("approval.cancelled", _HUMAN, "The run no longer needs the approval."),
    _spec("human.task.assigned", _HUMAN, "Work assigned to a person."),
    _spec("human.task.completed", _HUMAN, "A person completed assigned work."),
    _spec("policy.decision.denied", _SECURITY, "A governed action was denied."),
    _spec("budget.warning", "Model Kernel, Workflow Engine", "A soft budget limit was reached."),
    _spec(
        "budget.exceeded",
        "Model Kernel, Workflow Engine, Event Fabric",
        "A hard budget limit was hit.",
        BudgetExceeded,
    ),
    _spec("system.dlq.added", _FABRIC, "An event was dead-lettered (FR-EVT-08).", DeadLetterAdded),
    _spec("system.consumer.lagging", _FABRIC, "A consumer group is falling behind."),
)

_BY_TYPE = {spec.type: spec for spec in STANDARD_EVENTS}


def get(event_type: str) -> EventSpec | None:
    """The catalog entry for an event type, or ``None`` for App-defined types."""
    return _BY_TYPE.get(event_type)
