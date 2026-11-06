"""What the console shows, read from the runtime (WP 3.2).

None of this changes anything. Each view is for one tenant, the caller's, and the API decides who
may see it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, JsonValue

from aios.core.apps import Grants
from aios.core.envelope import Event, Principal
from aios.core.errors import NotFoundError, ToolDeniedError
from aios.core.identity import Suspendable
from aios.core.policy import Actor, PolicyAction, PolicyRequest, Resource, SideEffect
from aios.fabric.port import Scope
from aios.observability.compare import Comparison, compare
from aios.observability.costs import ALERTS, Costs, costs_of, month_after, month_start
from aios.observability.trace import Trace, trace_of

if TYPE_CHECKING:
    from aios.controlplane.runtime import Runtime

RECENT_TASKS = 200
"""How many of an App's newest task events an agent's figures are worked out from."""
CLOCK_SKEW = timedelta(hours=1)
"""The log is in the order events were stored; each process stamps its own time, so near the start
of a month the two can disagree a little. Reading on this far past it misses nothing."""


class AgentView(BaseModel):
    app: str
    agent: str
    version: str
    capabilities: list[str]
    tools: list[str]
    refused_tools: list[str] = Field(
        default_factory=list[str], description="On its list, but refused when the App was installed"
    )
    memory_read: list[str]
    memory_write: list[str]
    suspended: bool = False
    suspension: str | None = Field(default=None, description="Why, if it or its App is suspended")
    completed: int = Field(default=0, description="Among the App's recent tasks")
    failed: int = 0
    last_active: datetime | None = None


async def agents_of(runtime: Runtime, tenant: str) -> list[AgentView]:
    """Every agent of every App installed for ``tenant``, with how its recent work went."""
    stopped = {(s.kind, s.name): s for s in await runtime.security.suspensions(tenant)}
    found: list[AgentView] = []
    for (owner, name), installed in sorted(runtime.apps.items()):
        if owner != tenant:
            continue
        done: dict[str, int] = {}
        broke: dict[str, int] = {}
        last: dict[str, datetime] = {}
        seen = 0
        async for stored in runtime.bus.latest(Scope(tenant, name), "task.>"):
            event = stored.event
            agent = event.principal.split(":", 1)[-1].split("@", 1)[0]
            if event.type == "task.completed":
                done[agent] = done.get(agent, 0) + 1
            elif event.type == "task.failed":
                broke[agent] = broke.get(agent, 0) + 1
            if event.principal.startswith("agent:"):
                last.setdefault(agent, event.time)
            seen += 1
            if seen >= RECENT_TASKS:
                break
        for definition in installed.agents:
            why = (
                stopped.get((Suspendable.TENANT, tenant))
                or stopped.get((Suspendable.APP, name))
                or stopped.get((Suspendable.AGENT, definition.name))
            )
            found.append(
                AgentView(
                    app=name,
                    agent=definition.name,
                    version=definition.version,
                    capabilities=list(definition.capabilities),
                    tools=list(definition.tools.allow),
                    refused_tools=[
                        tool
                        for tool in definition.tools.allow
                        if installed.grants is not None
                        and not Grants(tools=list(installed.grants.tools)).covers(
                            Grants(tools=[tool])
                        )
                    ],
                    memory_read=list(definition.memory.read),
                    memory_write=list(definition.memory.write),
                    suspended=why is not None,
                    suspension=f"{why.kind.value} {why.name}: {why.reason}" if why else None,
                    completed=done.get(definition.name, 0),
                    failed=broke.get(definition.name, 0),
                    last_active=last.get(definition.name),
                )
            )
    return found


async def comparison(runtime: Runtime, tenant: str, app: str, a: str, b: str) -> Comparison:
    """Two runs side by side, step by step."""
    traces = [await trace(runtime, tenant, app, run) for run in (a, b)]
    outputs: list[JsonValue] = []
    for run in (a, b):
        ended = (await runtime.run_state(app, run, tenant=tenant)).ended
        data = ended.data if ended is not None and isinstance(ended.data, dict) else {}
        outputs.append(data.get("output"))
    return compare(traces[0], traces[1], (outputs[0], outputs[1]))


async def trace(runtime: Runtime, tenant: str, app: str, run_id: str) -> Trace:
    events = [
        stored.event async for stored in runtime.bus.replay(Scope(tenant, app), run_id=run_id)
    ]
    if not events:
        raise NotFoundError(f"no run {run_id}")
    started = next((e for e in events if e.type == "run.started"), None)
    data: dict[str, Any] = started.data if started and isinstance(started.data, dict) else {}
    definition = runtime.catalog.get(tenant, app)
    workflow = (
        definition.workflow(str(data.get("workflow")), data.get("version"))
        if definition is not None and data.get("workflow")
        else None
    )
    return trace_of(events, workflow, app=app)


Level = Literal["tenant", "app", "user"]


class Budget(BaseModel):
    level: Level
    subject: str
    limit_usd: float
    soft_usd: float
    spent_usd: float = 0.0


class CostReport(Costs):
    budgets: list[Budget] = Field(default_factory=list[Budget])


async def costs(runtime: Runtime, tenant: str, month: str) -> CostReport:
    """A month's spending, read newest first back to the month's start, and the limits on it."""
    start, end = month_start(month), month_after(month_start(month))
    events: list[Event] = []
    for pattern in ("model.call.completed", *ALERTS):
        async for stored in runtime.bus.latest(Scope(tenant), pattern):
            if stored.event.time < start - CLOCK_SKEW:
                break
            events.append(stored.event)
    report = CostReport(**costs_of(events, month).model_dump())
    if end <= datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0):
        return report  # the ledger keeps this month's totals, not those of months gone
    report.budgets = await _budgets(runtime, tenant, month, report)
    return report


async def _budgets(runtime: Runtime, tenant: str, month: str, report: Costs) -> list[Budget]:
    """Each App's limit and each person's, with the tightest tenant limit any App's policy sets."""
    found: dict[str, Budget] = {}
    tenant_limits: list[tuple[float, float]] = []
    for (owner, name), _ in sorted(runtime.apps.items()):
        if owner != tenant:
            continue
        limits, soft_at = runtime.models.budget_limits(tenant, name)
        if "app" in limits:
            subject = f"app:{name}:{month}"
            found[subject] = _budget("app", subject, limits["app"], soft_at)
        if "tenant" in limits:
            tenant_limits.append((limits["tenant"], soft_at))
        if "user" in limits:
            for spend in report.by_user:
                if spend.key.startswith(("user:", "service:")):
                    subject = f"user:{spend.key}:{month}"
                    found.setdefault(subject, _budget("user", subject, limits["user"], soft_at))
    if tenant_limits:
        limit, soft_at = min(tenant_limits)
        found[f"tenant:{month}"] = _budget("tenant", f"tenant:{month}", limit, soft_at)
    spent = await runtime.models.ledger.spent(tenant, list(found))
    for subject, budget in found.items():
        budget.spent_usd = round(spent.get(subject, 0.0), 8)
    return list(found.values())


def _budget(level: Level, subject: str, limit: float, soft_at: float) -> Budget:
    return Budget(level=level, subject=subject, limit_usd=limit, soft_usd=round(limit * soft_at, 8))


class PolicyView(BaseModel):
    app: str
    policies: list[dict[str, Any]]
    rules: list[dict[str, Any]]
    cedar: str


def policies_of(runtime: Runtime, tenant: str, app: str) -> PolicyView:
    found = runtime.enforcer.policy_set(tenant, app)
    if found is None:
        raise NotFoundError(f"App {app!r} is not installed")
    manifests, compiled = found
    own = {manifest.metadata.name for manifest in manifests}
    return PolicyView(
        app=app,
        policies=[m.model_dump(mode="json", by_alias=True, exclude_none=True) for m in manifests],
        rules=[
            {"id": rule.id, "kind": rule.kind, "approvers": list(rule.approvers)}
            for rule in compiled.rules
            if rule.id.split("/", 1)[0] in own
        ],
        cedar=compiled.text,
    )


class Simulation(BaseModel):
    """A question for the policy engine. Nothing happens, and nothing is recorded."""

    action: Literal["tool.invoke", "memory.read", "memory.write", "task.dispatch", "model.route"]
    agent: str = Field(min_length=1)
    resource: str = Field(default="", description="The tool, or the memory scope")
    side_effect: SideEffect | None = Field(
        default=None, description="For a tool this process doesn't know"
    )
    on_behalf_of: Principal | None = None
    tainted: bool = False
    approved: bool = False
    input: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])


class Verdict(BaseModel):
    effect: str
    policies: list[str]
    approvers: list[str]
    reason: str


async def simulate(runtime: Runtime, tenant: str, app: str, question: Simulation) -> Verdict:
    """Would this be allowed now? The kill switch counts, as it would for the real thing."""
    if runtime.enforcer.policy_set(tenant, app) is None:
        raise NotFoundError(f"App {app!r} is not installed")
    action = PolicyAction(question.action)
    if action is PolicyAction.TOOL_INVOKE:
        side_effect = question.side_effect
        if side_effect is None:
            try:
                spec = await runtime.tool_spec(question.resource, tenant)
            except ToolDeniedError as exc:  # blocked until approved again: so it would be refused
                return Verdict(effect="deny", policies=[], approvers=[], reason=str(exc))
            if spec is None:
                raise NotFoundError(
                    f"no tool {question.resource!r} is known here; say what its side effect is"
                )
            side_effect = spec.side_effect
        resource = Resource.tool(question.resource, side_effect)
    elif action in (PolicyAction.MEMORY_READ, PolicyAction.MEMORY_WRITE):
        resource = Resource.memory_scope(question.resource)
    else:
        resource = Resource.tenant(tenant)
    decision = await runtime.enforcer.decide(
        PolicyRequest(
            tenant=tenant,
            app=app,
            actor=Actor(f"agent:{question.agent}@{tenant}"),
            action=action,
            resource=resource,
            on_behalf_of=Actor(question.on_behalf_of) if question.on_behalf_of else None,
            tainted=question.tainted,
            approved=question.approved,
            input=question.input,
        )
    )
    return Verdict(
        effect=decision.effect.value,
        policies=list(decision.policies),
        approvers=list(decision.approvers),
        reason=decision.reason,
    )
