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
from aios.core.policy import OnTimeout
from aios.core.topics import CapabilityName, EventKind, SignalName, kind_of, validate_topic

Risk = Literal["low", "medium", "high"]


class Payload(BaseModel):
    """Base class for catalog payload models."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ErrorInfo(Payload):
    code: str = Field(min_length=1, description="Stable machine-readable code, e.g. tool.timeout.")
    message: str
    retryable: bool = False


class Usage(Payload):
    input_tokens: int = Field(default=0, ge=0, description="The whole prompt, cached or not.")
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(
        default=0, ge=0, description="Prompt tokens a provider served from its cache."
    )
    cache_write_tokens: int = Field(
        default=0, ge=0, description="Prompt tokens written to a provider's cache."
    )
    cost_usd: float = Field(default=0.0, ge=0)


class TaskBudget(Payload):
    max_steps: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0)
    timeout_s: float | None = Field(default=None, gt=0)


# --------------------------------------------------------------------------- task


class TaskSource(Payload):
    """Where a replayed, forked or retried task's run came from, as the components need it."""

    run: Ulid = Field(description="The run whose record answers this task's calls")
    mode: Literal["replay", "fork", "retry"]
    model: str | None = Field(default=None, description="A registry entry every call must use")
    live: bool = Field(
        default=False, description="Side effects happen for real. Off, they are stubbed."
    )


class TaskRequested(Payload):
    capability: CapabilityName
    input: JsonValue = None
    step: str | None = Field(
        default=None, description="Workflow step id, when the task belongs to a run."
    )
    item: int | None = Field(default=None, ge=0, description="Fan-out item index (FR-WF-03).")
    loop: int = Field(default=0, ge=0, description="Loop iteration of the step.")
    attempt: int = Field(default=1, ge=1, description="Attempt number of the step.")
    budget: TaskBudget | None = None
    resumes: list[Ulid] = Field(
        default_factory=list[Ulid],
        description="Earlier executions of this task, each stopped to wait for an approval.",
    )
    taint: list[str] = Field(
        default_factory=list[str], description="Where the run's untrusted content came from."
    )
    source: TaskSource | None = Field(
        default=None, description="Set in a replay, fork or retry (FR-OBS-04, FR-WF-11)."
    )


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


class TaskSuspended(Payload):
    capability: CapabilityName
    approval: Ulid = Field(description="What the task waits for. Nothing runs until it is decided.")


# --------------------------------------------------------------------------- app


class CopiedStep(Payload):
    step: str
    output: JsonValue = None
    loop: int = Field(default=0, ge=0)


class RunSource(Payload):
    """The run a replay, fork or retry comes from (FR-OBS-04, FR-WF-11).

    A **replay** runs every step again and answers each model and tool call from the source's
    record: nothing live, nothing spent. A **fork** keeps the source's results for the steps it
    doesn't start again, and runs ``step`` and what follows it live, with side effects stubbed.
    A **retry** is a fork with nothing stubbed.
    """

    run: Ulid
    mode: Literal["replay", "fork", "retry"]
    step: str | None = Field(default=None, description="Where a fork or retry starts again")
    copied: list[CopiedStep] = Field(
        default_factory=list[CopiedStep],
        description="Steps whose results are kept from the source, in the order they ran",
    )
    answers: list[CopiedStep] = Field(
        default_factory=list[CopiedStep],
        description="What people and signals said in the source, given when the run gets there",
    )
    taint: list[str] = Field(
        default_factory=list[str],
        description="The source's untrusted content: what a copy keeps, it keeps the taint of",
    )
    step_input: JsonValue = Field(default=None, description="A fork's new input for ``step``")
    model: str | None = Field(default=None, description="A registry entry every call must use")
    live: bool = Field(default=False, description="Side effects happen for real")


class AppRequestReceived(Payload):
    entrypoint: Literal["chat", "event", "schedule", "webhook", "replay", "fork", "retry"] = "chat"
    workflow: str | None = Field(
        default=None, description="Defaults to the workflow of the App's entrypoint."
    )
    version: str | None = Field(
        default=None, description="A workflow version still loaded. Defaults to the latest."
    )
    input: JsonValue = None
    source: RunSource | None = None


# --------------------------------------------------------------------------- run


class RunStarted(Payload):
    workflow: str = Field(min_length=1)
    version: str = Field(min_length=1)
    input: JsonValue = None
    source: RunSource | None = Field(
        default=None, description="The run this one replays, forks or retries"
    )


class RunCompleted(Payload):
    output: JsonValue = None
    usage: Usage | None = None


class RunFailed(Payload):
    error: ErrorInfo
    usage: Usage | None = None


class RunCancelRequested(Payload):
    reason: str = ""


class RunCancelled(Payload):
    reason: str = ""
    usage: Usage | None = None


class RunPauseRequested(Payload):
    reason: str = ""


class RunResumeRequested(Payload):
    reason: str = ""


class RunSignalRequested(Payload):
    name: SignalName
    data: JsonValue = Field(default=None, description="Becomes the wait step's output.")


class RunPaused(Payload):
    reason: Literal["request", "approval", "signal"] = Field(
        description="What the run waits for. Only a requested pause holds all of its new work."
    )
    step: str | None = Field(default=None, description="The waiting step, unless requested.")
    item: int | None = Field(default=None, ge=0, description="The waiting fan-out item, if any.")


class RunResumed(Payload):
    reason: Literal["request", "approval", "signal"]
    step: str | None = None
    item: int | None = Field(default=None, ge=0)


# --------------------------------------------------------------------------- step


class StepStarted(Payload):
    step: str
    attempt: int = Field(default=1, ge=1)
    loop: int = Field(default=0, ge=0)
    items: int | None = Field(default=None, ge=0, description="Fan-out size, for dynamic steps.")


class StepCompleted(Payload):
    copied_from: Ulid | None = Field(
        default=None, description="Not run here: the result is the one this run recorded"
    )
    taint: list[str] = Field(
        default_factory=list[str], description="The untrusted content a copied result came from"
    )
    step: str
    output: JsonValue = None
    loop: int = Field(default=0, ge=0)
    looped: bool = Field(
        default=False, description="The result sent the run back to an earlier step (onResult)."
    )
    loops_exhausted: bool = False
    signal: Ulid | None = Field(default=None, description="The signal a wait step took.")


class StepFailed(Payload):
    step: str
    error: ErrorInfo


class StepRetrying(Payload):
    step: str
    attempt: int = Field(ge=2, description="The attempt about to start.")
    error: ErrorInfo


# --------------------------------------------------------------------------- agent


class AgentLifecycle(Payload):
    agent: str
    instance: str
    capabilities: list[str] = []


class EmitStubbed(Payload):
    """A fact an agent would have published, in a replay or fork that makes nothing happen."""

    type: str
    data: JsonValue = None


# --------------------------------------------------------------------------- model, tool


class ModelCallCompleted(Payload):
    call: int = Field(ge=0, description="Index of the call within the task execution.")
    site: str = Field(description="Call-site key (aios.core.ports.CallSite.key).")
    agent: str
    step: str | None = None
    task: str | None = Field(default=None, description="Routing hint the policy resolved.")
    operation: Literal["generate", "embed"] = "generate"
    provider: str
    model: str
    usage: Usage
    latency_ms: float = Field(ge=0)
    stop_reason: str | None = None
    output_text: str = Field(
        description="The recorded completion (ADR-010), for replay. Empty for embeddings."
    )
    output: JsonValue = Field(
        default=None,
        description="Parsed structured output, or the size of an embedding (never its vectors).",
    )
    fallback_from: list[str] = Field(
        default_factory=list, description="Candidates that failed before this one."
    )
    downgraded: bool = Field(default=False, description="A soft budget limit chose this model.")
    replayed: bool = Field(
        default=False,
        description="Answered from a recorded run: nothing called, nothing spent",
    )
    request_hash: str | None = Field(
        default=None, description="What was asked, so a replay can tell when it asks otherwise"
    )


class ModelCallFailed(Payload):
    call: int = Field(ge=0)
    site: str
    agent: str
    step: str | None = None
    task: str | None = None
    error: ErrorInfo
    candidates: list[str] = []


class ToolCallRequested(Payload):
    call: int = Field(ge=0)
    site: str
    tool: str
    agent: str
    step: str | None = None
    arguments: JsonValue = None
    idempotency_key: str | None = None


class ToolCallCompleted(Payload):
    call: int = Field(ge=0)
    site: str
    tool: str
    provider: str
    latency_ms: float = Field(ge=0)
    output: JsonValue = None
    untrusted: bool = True
    stubbed: bool = Field(
        default=False, description="Not run: a replay or fork held the side effect back"
    )
    replayed: bool = Field(default=False, description="Answered from a recorded run")


class ToolCallFailed(Payload):
    call: int = Field(ge=0)
    site: str
    tool: str
    error: ErrorInfo


# --------------------------------------------------------------------------- memory
#
# These never quote an item or a query: the log is append-only, and forget must really erase.


class MemoryWritten(Payload):
    item: Ulid
    scope: str
    kind: str
    retention: str
    sensitivity: str
    version: int = Field(default=1, ge=1)
    site: str | None = Field(
        default=None, description="Where in its task an agent wrote it, so it is written once."
    )
    stubbed: bool = Field(
        default=False, description="Not stored: a replay or fork held the write back"
    )


class MemoryRecalled(Payload):
    operation: Literal["recall", "search", "get"]
    scopes: list[str]
    items: list[Ulid] = Field(description="The items the caller received.")
    mode: str = "keyword"
    policies: list[str] = Field(default_factory=list[str], description="The rules that allowed it.")


class MemoryForgotten(Payload):
    items: list[Ulid]
    held: int = Field(default=0, ge=0, description="Items kept because of a legal hold.")
    subject_hash: str | None = Field(
        default=None,
        description="SHA-256 of the erased subject: proof for whoever already knows the subject.",
    )


class MemoryIngestStarted(Payload):
    source: str
    documents: int = Field(ge=0)
    scope: str


class MemoryDocumentSynced(Payload):
    source: str
    scope: str
    added: int = Field(default=0, ge=0)
    updated: int = Field(default=0, ge=0)
    unchanged: int = Field(default=0, ge=0)
    removed: int = Field(default=0, ge=0)
    chunks: int = Field(default=0, ge=0)
    embedded: bool = Field(default=False, description="Chunks have vectors, not just keywords.")


# --------------------------------------------------------------------------- approval


class ApprovalRequested(Payload):
    """What an approver sees (FR-HUM-03)."""

    approval: Ulid
    approvers: list[str] = Field(min_length=1, description="Principals or role:<name> selectors.")
    reason: str
    summary: str = Field(description="What would happen, in one line.")
    action: str | None = Field(
        default=None, description="The tool, unless this is a workflow step."
    )
    payload: JsonValue = Field(default=None, description="Exactly what is being approved.")
    risk: Risk
    policies: list[str] = Field(default_factory=list[str], description="The rules that asked.")
    rationale: str | None = Field(
        default=None, description="The agent's reason, in its own words. A claim, not evidence."
    )
    tainted: bool = Field(default=False, description="The agent had read untrusted content.")
    taint: list[str] = Field(default_factory=list[str], description="Where that content came from.")
    step: str | None = None
    item: int | None = Field(default=None, ge=0)
    expires_at: AwareDatetime
    on_timeout: OnTimeout = "deny"


class ApprovalDecided(Payload):
    approval: Ulid
    by: Principal
    comment: str | None = None


class ApprovalExpired(Payload):
    approval: Ulid
    approved: bool = Field(default=False, description="A policy let it go ahead without a person.")


class ApprovalCancelled(Payload):
    approval: Ulid
    reason: str = ""


# --------------------------------------------------------------------------- security


class PolicyDecided(Payload):
    action: str = Field(description="The governed action, e.g. tool.invoke.")
    resource: str = Field(description="What it applies to, e.g. stripe.refund.create.")
    policies: list[str] = Field(default_factory=list[str], description="The rules that decided.")
    approvers: list[str] = Field(default_factory=list[str])
    reason: str = ""
    approval: Ulid | None = Field(default=None, description="The approval this decision relied on.")


class InjectionSuspected(Payload):
    """Never quotes the content: the log is permanent, and this may be someone's data."""

    source: str = Field(description="Where the content came from, e.g. tool:web.page.read.")
    classifier: str
    score: float = Field(ge=0, le=1)
    rules: list[str] = Field(default_factory=list[str], description="What it noticed.")


class KeyChanged(Payload):
    key: Ulid
    name: str
    roles: list[str] = Field(default_factory=list[str])


class SuspensionChanged(Payload):
    kind: Literal["agent", "app", "tenant"]
    name: str
    reason: str = ""


# --------------------------------------------------------------------------- installations


class AppChanged(Payload):
    app: str
    version: str
    previous: str | None = None
    digest: str | None = None
    by: str


class AppConsentRequested(Payload):
    """What an App asks for, so that the record shows what somebody agreed to (FR-APP-03)."""

    app: str
    version: str
    requested: dict[str, JsonValue]


class AppUninstalled(Payload):
    app: str
    version: str | None = None
    data: Literal["retain", "delete"]
    by: str


# --------------------------------------------------------------------------- budget, system


BudgetLevel = Literal["run", "user", "app", "tenant"]


class BudgetExceeded(Payload):
    budget: Literal["hops", "steps", "tokens", "cost", "time", "events"]
    limit: float
    observed: float
    level: BudgetLevel | None = Field(default=None, description="Whose money ran out.")


class BudgetWarning(Payload):
    level: BudgetLevel
    limit: float = Field(description="The hard limit, in USD.")
    soft_limit: float
    observed: float
    action: Literal["downgrade", "warn", "deny"] = Field(
        description="What the model policy does from here on."
    )


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
    _spec(
        "app.request.received",
        _ACCESS,
        "A user or trigger asked an App for something.",
        AppRequestReceived,
    ),
    _spec("app.response.ready", _ACCESS, "The App's response is ready to stream to the caller."),
    _spec(
        "app.consent.requested",
        _CONTROL,
        "An App is waiting for a tenant admin to agree.",
        AppConsentRequested,
    ),
    _spec("app.consent.refused", _CONTROL, "A tenant admin said no to an App.", AppChanged),
    _spec("app.installed", _CONTROL, "An App was installed into a tenant.", AppChanged),
    _spec("app.upgraded", _CONTROL, "An installed App moved to a new version.", AppChanged),
    _spec("app.version.restored", _CONTROL, "An App went back to an earlier version.", AppChanged),
    _spec("app.config.changed", _CONTROL, "An App's configuration changed.", AppChanged),
    _spec("app.uninstalled", _CONTROL, "An App was removed from a tenant.", AppUninstalled),
    _spec("run.started", _WORKFLOW, "A workflow run started.", RunStarted),
    _spec("run.paused", _WORKFLOW, "A run is waiting (request, approval, signal).", RunPaused),
    _spec("run.resumed", _WORKFLOW, "A paused run continued.", RunResumed),
    _spec("run.completed", _WORKFLOW, "A run finished successfully.", RunCompleted),
    _spec("run.failed", _WORKFLOW, "A run failed.", RunFailed),
    _spec(
        "run.cancel.requested",
        f"{_ACCESS}, {_CONTROL}",
        "Asks to cancel a run.",
        RunCancelRequested,
    ),
    _spec("run.cancelled", _WORKFLOW, "A run was cancelled.", RunCancelled),
    _spec(
        "run.pause.requested",
        f"{_ACCESS}, {_CONTROL}",
        "Asks a run to start no new work until it is resumed.",
        RunPauseRequested,
    ),
    _spec(
        "run.resume.requested",
        f"{_ACCESS}, {_CONTROL}",
        "Asks a held run to continue.",
        RunResumeRequested,
    ),
    _spec(
        "run.signal.requested",
        f"{_ACCESS}, {_CONTROL}",
        "External input for a run (FR-WF-08); the run keeps it until a wait step takes it.",
        RunSignalRequested,
    ),
    _spec("step.scheduled", _WORKFLOW, "A step is ready to run."),
    _spec("step.started", _WORKFLOW, "A step started.", StepStarted),
    _spec("step.completed", _WORKFLOW, "A step finished; its output is recorded.", StepCompleted),
    _spec("step.failed", _WORKFLOW, "A step failed.", StepFailed),
    _spec("step.retrying", _WORKFLOW, "A failed step will be retried.", StepRetrying),
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
    _spec(
        "task.suspended",
        _KERNEL,
        "The task stopped to wait for an approval; it runs again once that is decided.",
        TaskSuspended,
    ),
    _spec(
        "agent.lifecycle.registered",
        _KERNEL,
        "Agent lifecycle transition (FR-AGT-03).",
        AgentLifecycle,
    ),
    _spec("agent.lifecycle.starting", _KERNEL, "Agent lifecycle transition.", AgentLifecycle),
    _spec("agent.lifecycle.ready", _KERNEL, "Agent lifecycle transition.", AgentLifecycle),
    _spec("agent.lifecycle.busy", _KERNEL, "Agent lifecycle transition.", AgentLifecycle),
    _spec("agent.lifecycle.suspended", _KERNEL, "Agent lifecycle transition.", AgentLifecycle),
    _spec("agent.lifecycle.draining", _KERNEL, "Agent lifecycle transition.", AgentLifecycle),
    _spec("agent.lifecycle.stopped", _KERNEL, "Agent lifecycle transition.", AgentLifecycle),
    _spec("agent.lifecycle.failed", _KERNEL, "Agent lifecycle transition.", AgentLifecycle),
    _spec(
        "agent.emit.stubbed",
        _KERNEL,
        "A replay or fork held back a fact an agent emitted, so that nothing it triggers happens.",
        EmitStubbed,
    ),
    _spec(
        "model.call.completed",
        _MODELS,
        "Summary of a model call: model, tokens, cost, and the recorded output.",
        ModelCallCompleted,
    ),
    _spec("model.call.failed", _MODELS, "A model call failed after fallbacks.", ModelCallFailed),
    _spec(
        "tool.call.requested",
        _TOOLS,
        "A tool invocation, before the policy decision (an audit record of the request).",
        ToolCallRequested,
    ),
    _spec("tool.call.completed", _TOOLS, "A tool returned.", ToolCallCompleted),
    _spec("tool.call.failed", _TOOLS, "A tool failed or was denied.", ToolCallFailed),
    _spec("memory.item.written", _MEMORY, "A memory item was stored.", MemoryWritten),
    _spec("memory.item.updated", _MEMORY, "A memory item has a new version.", MemoryWritten),
    _spec("memory.recalled", _MEMORY, "Someone received memory items.", MemoryRecalled),
    _spec("memory.item.forgotten", _MEMORY, "Memory items were erased.", MemoryForgotten),
    _spec("memory.ingest.started", _MEMORY, "A document sync began.", MemoryIngestStarted),
    _spec("memory.document.synced", _MEMORY, "A document sync finished.", MemoryDocumentSynced),
    _spec(
        "approval.requested",
        f"{_WORKFLOW}, {_SECURITY}",
        "Asks Human OS for a decision; the step waiting for it stops until it arrives.",
        ApprovalRequested,
    ),
    _spec("approval.granted", _HUMAN, "The approval was granted.", ApprovalDecided),
    _spec("approval.denied", _HUMAN, "The approval was denied.", ApprovalDecided),
    _spec("approval.modified", _HUMAN, "The approver changed the proposed action."),
    _spec("approval.expired", _HUMAN, "No decision before the timeout.", ApprovalExpired),
    _spec("approval.escalated", _HUMAN, "The approval moved to the next approver tier."),
    _spec("approval.cancelled", _HUMAN, "The run no longer needs the approval.", ApprovalCancelled),
    _spec("human.task.assigned", _HUMAN, "Work assigned to a person."),
    _spec("human.task.completed", _HUMAN, "A person completed assigned work."),
    _spec("policy.decision.allowed", _SECURITY, "A governed action was allowed.", PolicyDecided),
    _spec("policy.decision.denied", _SECURITY, "A governed action was denied.", PolicyDecided),
    _spec(
        "policy.decision.escalated",
        _SECURITY,
        "A governed action needs an approval first.",
        PolicyDecided,
    ),
    _spec(
        "security.injection.suspected",
        _SECURITY,
        "Content reaching an agent looks like an injection (FR-SEC-09).",
        InjectionSuspected,
    ),
    _spec("security.key.created", _SECURITY, "An API key was created.", KeyChanged),
    _spec("security.key.revoked", _SECURITY, "An API key was revoked.", KeyChanged),
    _spec(
        "security.suspended",
        _SECURITY,
        "The kill switch stopped an agent, an App or a tenant (FR-SEC-13).",
        SuspensionChanged,
    ),
    _spec("security.resumed", _SECURITY, "A suspension ended.", SuspensionChanged),
    _spec("budget.warning", "Model Kernel", "A soft budget limit was reached.", BudgetWarning),
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
