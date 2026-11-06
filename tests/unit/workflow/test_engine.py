import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, cast

import pytest
from pydantic import JsonValue

from aios.core.envelope import Event
from aios.core.ids import new_ulid
from aios.core.manifests import (
    AppManifest,
    Limits,
    WorkflowManifest,
    load_manifests,
    parse_manifest,
)
from aios.core.ports import (
    CallSite,
    EmbeddingRequest,
    EmbeddingResponse,
    ModelGateway,
    ModelRequest,
    ModelResponse,
    ToolGateway,
    ToolResult,
)
from aios.fabric import InMemoryEventBus, Scope
from aios.kernel import AgentDefinition, Context, Installation, Worker, agent, definition_of
from aios.sdk import Step, Wait, Workflow
from aios.workflow import TOOL_CAPABILITY, AppDefinition, WorkflowCatalog, WorkflowEngine

TENANT, APP = "acme", "demo"
SCOPE = Scope(TENANT, APP)


class NoModels(ModelGateway):
    async def generate(self, request: ModelRequest, site: CallSite) -> ModelResponse:
        raise AssertionError("no model calls in engine tests")

    async def embed(self, request: EmbeddingRequest, site: CallSite) -> EmbeddingResponse:
        raise AssertionError("no model calls in engine tests")


class EchoTools(ToolGateway):
    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, JsonValue],
        site: CallSite,
        *,
        idempotency_key: str | None = None,
        rationale: str | None = None,
    ) -> ToolResult:
        return ToolResult(
            tool=name, output={"tool": name, **arguments}, provider="echo", untrusted=False
        )


@agent("planner", capability="planning")
async def planner(ctx: Context, task: Any) -> dict[str, Any]:
    return {"tasks": [{"q": f"{task['question']} #{i}"} for i in range(3)]}


@agent("researcher", capability="research.web")
async def researcher(ctx: Context, task: dict[str, Any]) -> dict[str, Any]:
    await asyncio.sleep(0.01)
    return {"finding": task["q"].upper()}


@agent("writer", capability="writing")
async def writer(ctx: Context, task: Any) -> dict[str, Any]:
    feedback = cast(dict[str, Any], task).get("feedback") if isinstance(task, dict) else None
    return {"draft": f"report (revised: {feedback is not None})"}


def reviewer(decisions: Sequence[str]) -> AgentDefinition:
    queue = list(decisions)

    @agent("reviewer", capability="review")
    async def review(ctx: Context, task: Any) -> dict[str, str]:
        return {"result": queue.pop(0) if queue else "approve"}

    return definition_of(review)


@agent("tool-runner", capability=TOOL_CAPABILITY)
async def tool_runner(ctx: Context, task: dict[str, Any]) -> Any:
    return await ctx.tools.invoke(task["tool"], **task["input"])


RESEARCH = """
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: research, version: 1.0.0}
spec:
  output: ${draft.output}
  steps:
    - {id: plan, type: task, capability: planning, input: {question: "${input.question}"}}
    - {id: research, type: dynamic, capability: research.*, fromPlan: plan.output.tasks,
       maxParallel: 2}
    - {id: draft, type: task, capability: writing}
    - {id: review, type: task, capability: review, onResult: {reject: draft}, maxLoops: 2}
"""


def _workflow(text: str) -> WorkflowManifest:
    [manifest] = load_manifests(text)
    assert isinstance(manifest, WorkflowManifest)
    return manifest


@dataclass
class Harness:
    bus: InMemoryEventBus
    catalog: WorkflowCatalog
    worker: Worker
    engine: WorkflowEngine

    async def start_run(self, data: dict[str, Any] | None = None, **request: Any) -> str:
        run_id = new_ulid()
        await self.bus.publish(
            Event(
                source=f"aios://{TENANT}/apps/{APP}/api",
                type="app.request.received",
                tenantid=TENANT,
                appid=APP,
                runid=run_id,
                correlationid=run_id,
                principal=f"user:alice@{TENANT}",
                data={"entrypoint": "chat", "input": data or {"question": "why"}, **request},
            )
        )
        return run_id

    async def events(self, run_id: str) -> list[Event]:
        return [s.event async for s in self.bus.replay(SCOPE, run_id=run_id)]

    async def command(self, run_id: str, type: str, **data: JsonValue) -> None:
        await self.bus.publish(
            Event(
                source=f"aios://{TENANT}/apps/{APP}/api",
                type=type,
                tenantid=TENANT,
                appid=APP,
                runid=run_id,
                correlationid=run_id,
                principal=f"user:alice@{TENANT}",
                data=data,
            )
        )

    async def restart_engine(self) -> None:
        await self.engine.kill()
        self.engine = WorkflowEngine(self.bus, self.catalog, lease=0.5)
        await self.engine.start()

    async def settled(self, run_id: str, type: str, count: int = 1) -> list[Event]:
        """Wait for ``count`` events of ``type``, and for the engine to have handled them."""
        async with asyncio.timeout(5):
            while True:
                if len(_types(await self.events(run_id), type)) >= count:
                    break
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        return await self.events(run_id)

    async def claimed(self, run_id: str, capability: str) -> None:
        """Wait until an agent has claimed a task of ``capability``."""
        async with asyncio.timeout(5):
            while True:
                events = await self.events(run_id)
                if any(
                    e.type == "task.claimed" and _data(e).get("capability") == capability
                    for e in events
                ):
                    return
                await asyncio.sleep(0.01)

    async def wait(self, run_id: str, *types: str, timeout: float = 5.0) -> list[Event]:
        wanted = types or ("run.completed", "run.failed", "run.cancelled")
        async with asyncio.timeout(timeout):
            while True:
                events = await self.events(run_id)
                if any(e.type in wanted for e in events):
                    return events
                await asyncio.sleep(0.01)


@pytest.fixture
async def harness() -> AsyncIterator[Any]:
    started: list[Harness] = []

    async def make(workflow: str, *agents: AgentDefinition, **budget: Any) -> Harness:
        manifest = _workflow(workflow)
        if budget:
            spec = manifest.spec.model_copy(
                update={"budget": manifest.spec.budget.model_copy(update=budget)}
            )
            manifest = manifest.model_copy(update={"spec": spec})
        app = parse_manifest(
            {
                "apiVersion": "aios/v1alpha1",
                "kind": "App",
                "metadata": {"name": APP, "version": "0.1.0"},
                "spec": {
                    "entrypoints": [
                        {"type": "chat", "workflow": manifest.metadata.name},
                        {
                            "type": "event",
                            "on": "ticket.created",
                            "workflow": manifest.metadata.name,
                        },
                    ],
                    "workflows": [manifest.metadata.name],
                },
            }
        )
        assert isinstance(app, AppManifest)
        bus = InMemoryEventBus()
        catalog = WorkflowCatalog()
        capabilities = frozenset(c for a in agents for c in a.capabilities)
        versions = {(manifest.metadata.name, manifest.metadata.version): manifest}
        catalog.install(
            TENANT, AppDefinition(app, {manifest.metadata.name: manifest}, capabilities, versions)
        )
        worker = Worker(
            bus,
            models=NoModels(),
            tools=EchoTools(),
            installations=[Installation(TENANT, APP, agents)],
            lease=0.5,
        )
        engine = WorkflowEngine(bus, catalog, lease=0.5)
        await worker.start()
        await engine.start()
        created = Harness(bus, catalog, worker, engine)
        started.append(created)
        return created

    yield make
    for created in started:
        await created.engine.kill()
        await created.worker.stop()
        await created.bus.close()


def _types(events: list[Event], prefix: str = "") -> list[str]:
    return [e.type for e in events if e.type.startswith(prefix)]


def _data(event: Event) -> dict[str, Any]:
    assert isinstance(event.data, dict)
    return event.data


def _agents(*extra: AgentDefinition) -> list[AgentDefinition]:
    research = replace(definition_of(researcher), limits=Limits(concurrency=5))
    return [definition_of(planner), research, definition_of(writer), *extra]


async def test_runs_a_workflow_end_to_end(harness: Any) -> None:
    h = await harness(RESEARCH, *_agents(reviewer([])))
    run_id = await h.start_run()
    events = await h.wait(run_id)
    assert events[-1].type == "run.completed"
    assert _data(events[-1])["output"] == {"draft": "report (revised: False)"}
    capabilities = [_data(e)["capability"] for e in events if e.type == "task.requested"]
    assert capabilities == [
        "planning",
        "research.web",
        "research.web",
        "research.web",
        "writing",
        "review",
    ]
    completed = [_data(e)["step"] for e in events if e.type == "step.completed"]
    assert completed == ["plan", "research", "draft", "review"]
    research = next(
        e for e in events if e.type == "step.completed" and _data(e)["step"] == "research"
    )
    assert _data(research)["output"] == [{"finding": f"WHY #{i}"} for i in range(3)]
    ids = {e.id for e in events}
    assert all(e.type == "app.request.received" or e.causationid in ids for e in events)
    started = next(e for e in events if e.type == "run.started")
    assert started.traceparent is not None
    assert all(e.traceparent == started.traceparent for e in events[1:])  # one trace per run


async def test_fan_out_respects_max_parallel(harness: Any) -> None:
    h = await harness(RESEARCH, *_agents(reviewer([])))
    run_id = await h.start_run()
    events = await h.wait(run_id)
    running = peak = 0
    for event in events:
        data = _data(event) if isinstance(event.data, dict) else {}
        if event.type == "task.requested" and data.get("step") == "research":
            running += 1
            peak = max(peak, running)
        elif event.type == "task.completed" and data.get("capability") == "research.web":
            running -= 1
    assert peak == 2


async def test_rejections_loop_back_within_max_loops(harness: Any) -> None:
    h = await harness(RESEARCH, *_agents(reviewer(["reject", "approve"])))
    events = await h.wait(await h.start_run())
    assert _types(events)[-1] == "run.completed"
    assert [_data(e)["step"] for e in events if e.type == "step.completed"] == [
        "plan",
        "research",
        "draft",
        "review",
        "draft",
        "review",
    ]
    assert _data(events[-1])["output"] == {"draft": "report (revised: True)"}
    loops = [
        _data(e)["loop"]
        for e in events
        if e.type == "task.requested" and _data(e)["step"] == "draft"
    ]
    assert loops == [0, 1]


async def test_loops_stop_at_max_loops(harness: Any) -> None:
    h = await harness(RESEARCH, *_agents(reviewer(["reject"] * 5)))
    events = await h.wait(await h.start_run())
    reviews = [e for e in events if e.type == "step.completed" and _data(e)["step"] == "review"]
    assert [_data(r)["looped"] for r in reviews] == [True, True, False]
    assert _data(reviews[-1])["loops_exhausted"] is True
    assert events[-1].type == "run.completed"


async def test_failed_tasks_are_retried_then_fail_the_run(harness: Any) -> None:
    attempts: list[int] = []

    @agent("flaky", capability="planning")
    async def flaky(ctx: Context, task: Any) -> dict[str, Any]:
        attempts.append(ctx.attempt)
        if ctx.attempt < 2:
            raise RuntimeError("transient")
        return {"tasks": []}

    h = await harness(RESEARCH, definition_of(flaky), *_agents(reviewer([]))[1:])
    events = await h.wait(await h.start_run())
    assert attempts == [1, 2]
    assert "step.retrying" in _types(events)
    assert events[-1].type == "run.completed"  # an empty plan fans out to nothing

    @agent("broken", capability="planning")
    async def broken(ctx: Context, task: Any) -> None:
        raise RuntimeError("always")

    h2 = await harness(RESEARCH, definition_of(broken), *_agents(reviewer([]))[1:])
    events = await h2.wait(await h2.start_run())
    assert _types(events, "step.")[-1] == "step.failed"
    assert events[-1].type == "run.failed"
    assert len([e for e in events if e.type == "task.requested"]) == 3  # maxAttempts


async def test_the_step_budget_stops_the_run(harness: Any) -> None:
    h = await harness(RESEARCH, *_agents(reviewer([])), max_steps=3)
    events = await h.wait(await h.start_run())
    engine_events = [e for e in events if e.principal == "system:workflow"]
    assert [e.type for e in engine_events][-2:] == ["budget.exceeded", "run.failed"]
    assert _data(engine_events[-2]) == {
        "budget": "steps",
        "limit": 3.0,
        "observed": 4.0,
        "level": None,
    }


async def test_cancellation(harness: Any) -> None:
    gate = asyncio.Event()

    @agent("slow-planner", capability="planning")
    async def slow(ctx: Context, task: Any) -> dict[str, Any]:
        await gate.wait()
        return {"tasks": []}

    h = await harness(RESEARCH, definition_of(slow), *_agents(reviewer([]))[1:])
    run_id = await h.start_run()
    events = await h.wait(run_id, "task.claimed")
    started = next(e for e in events if e.type == "run.started")
    await h.bus.publish(
        started.derive(
            "run.cancel.requested",
            {"reason": "user"},
            source=f"aios://{TENANT}/api",
            principal=f"user:alice@{TENANT}",
        )
    )
    events = await h.wait(run_id, "run.cancelled")
    gate.set()
    await asyncio.sleep(0.1)
    events = await h.events(run_id)
    assert "run.completed" not in _types(events)
    assert not [e for e in events if e.type == "step.completed"]


async def test_an_engine_crash_loses_nothing_and_repeats_nothing(harness: Any) -> None:
    gate = asyncio.Event()

    @agent("gated-writer", capability="writing")
    async def gated(ctx: Context, task: Any) -> dict[str, str]:
        await gate.wait()
        return {"draft": "ok"}

    h = await harness(RESEARCH, *_agents()[:2], definition_of(gated), reviewer([]))
    run_id = await h.start_run()
    await h.claimed(run_id, "writing")
    await h.engine.kill()  # the engine dies while the writer works
    gate.set()
    await asyncio.sleep(0.1)  # task.completed arrives with no engine running
    restarted = WorkflowEngine(h.bus, h.catalog, lease=0.5)
    await restarted.start()
    events = await h.wait(run_id)
    await restarted.kill()
    assert events[-1].type == "run.completed"
    steps = [_data(e)["step"] for e in events if e.type == "step.completed"]
    assert steps == ["plan", "research", "draft", "review"]
    assert len([e for e in events if e.type == "task.requested"]) == 6


async def test_a_redelivered_trigger_changes_nothing(harness: Any) -> None:
    h = await harness(RESEARCH, *_agents(reviewer([])))
    run_id = await h.start_run()
    events = await h.wait(run_id)
    before = len(events)
    for event in events:
        await h.engine._handle(event)  # every trigger, delivered again
    assert len(await h.events(run_id)) == before


async def test_unknown_apps_and_workflows_fail_the_run(harness: Any) -> None:
    h = await harness(RESEARCH, *_agents(reviewer([])))
    run_id = await h.start_run(workflow="missing")
    events = await h.wait(run_id)
    assert events[-1].type == "run.failed"
    assert _data(events[-1])["error"]["code"] == "workflow.not_found"


TIMED = """
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: timed, version: 1.0.0}
spec:
  steps:
    - {id: plan, type: task, capability: planning, timeout: 500ms}
"""


async def test_step_timeouts_fail_the_run(harness: Any) -> None:
    @agent("stuck", capability="planning")
    async def stuck(ctx: Context, task: Any) -> None:
        await asyncio.sleep(30)

    h = await harness(TIMED, definition_of(stuck))
    events = await h.wait(await h.start_run(), timeout=5)
    assert _types(events)[-2:] == ["step.failed", "run.failed"]
    assert _data(events[-1])["error"]["code"] == "step.timeout"


APPROVAL = """
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: gated, version: 1.0.0}
spec:
  steps:
    - {id: approve, type: approval, approvers: ["role:editor"]}
    - {id: publish, type: tool, tool: cms.page.publish, input: {title: "${input.question}"}}
"""


async def test_approval_and_tool_steps(harness: Any) -> None:
    h = await harness(APPROVAL, definition_of(tool_runner))
    run_id = await h.start_run()
    events = await h.wait(run_id, "run.paused")
    request = next(e for e in events if e.type == "approval.requested")
    await h.bus.publish(
        request.derive(
            "approval.granted",
            {"approval": _data(request)["approval"], "by": "user:eve@acme"},
            source=f"aios://{TENANT}/system/human",
            principal="user:eve@acme",
        )
    )
    events = await h.wait(run_id)
    assert _types(events, "run.") == ["run.started", "run.paused", "run.resumed", "run.completed"]
    task = next(e for e in events if e.type == "task.requested")
    assert _data(task)["capability"] == TOOL_CAPABILITY
    assert _data(events[-1])["output"] == {"tool": "cms.page.publish", "title": "why"}


def test_the_dsl_compiles_to_the_same_ir() -> None:
    compiled = (
        Workflow("research", version="1.0.0")
        .then(Step("plan", capability="planning", input={"question": "${input.question}"}))
        .then(
            Step.dynamic(
                "research", from_plan="plan.output.tasks", capability="research.*", max_parallel=2
            )
        )
        .then(Step("draft", capability="writing"))
        .then(Step("review", capability="review", on_result={"reject": "draft"}, max_loops=2))
        .output("${draft.output}")
        .compile()
    )
    assert compiled.spec.model_dump() == _workflow(RESEARCH).spec.model_dump()


RESEARCH_V2 = """
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: research, version: 2.0.0}
spec:
  steps:
    - {id: plan, type: task, capability: planning, input: {question: "${input.question}"}}
"""


async def test_event_entrypoints_start_one_run_per_event(harness: Any) -> None:
    h = await harness(RESEARCH, *_agents(reviewer([])))
    ticket = Event(
        source=f"aios://{TENANT}/apps/{APP}/connectors/zendesk",
        type="ticket.created",
        tenantid=TENANT,
        appid=APP,
        principal=f"service:zendesk@{TENANT}",
        data={"question": "refund?"},
    )
    await h.bus.publish(ticket)
    async with asyncio.timeout(5):
        while True:
            requests = [s.event async for s in h.bus.replay(SCOPE, "app.request.received")]
            if requests:
                break
            await asyncio.sleep(0.01)
    [request] = requests
    assert request.causationid == ticket.id
    assert _data(request)["entrypoint"] == "event"
    assert request.runid is not None
    events = await h.wait(request.runid)
    assert events[-1].type == "run.completed"
    await h.engine._start_from(ticket, "research", 1)  # the same event, delivered again
    assert len([s async for s in h.bus.replay(SCOPE, "app.request.received")]) == 1


async def test_in_flight_runs_finish_on_the_version_they_started_with(harness: Any) -> None:
    gate = asyncio.Event()

    @agent("gated-planner", capability="planning")
    async def gated(ctx: Context, task: Any) -> dict[str, Any]:
        await gate.wait()
        return {"tasks": []}

    h = await harness(RESEARCH, definition_of(gated), *_agents(reviewer([]))[1:])
    old_run = await h.start_run()
    await h.claimed(old_run, "planning")
    v2 = _workflow(RESEARCH_V2)
    current = h.catalog.get(TENANT, APP)
    assert current is not None
    h.catalog.install(
        TENANT,
        AppDefinition(
            current.manifest,
            {"research": v2},
            current.capabilities,
            {**current.versions, ("research", "2.0.0"): v2},
        ),
    )
    gate.set()
    old = await h.wait(old_run)
    assert [_data(e)["step"] for e in old if e.type == "step.completed"] == [
        "plan",
        "research",
        "draft",
        "review",
    ]
    new = await h.wait(await h.start_run())
    assert next(e for e in new if e.type == "run.started").data == {
        "workflow": "research",
        "version": "2.0.0",
        "input": {"question": "why"},
        "source": None,  # not a replay, fork or retry
    }
    assert [_data(e)["step"] for e in new if e.type == "step.completed"] == ["plan"]


# --------------------------------------------------------------------------- pause and resume

TWO_STEPS = """
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: two-steps, version: 1.0.0}
spec:
  steps:
    - {id: first, type: task, capability: first}
    - {id: second, type: task, capability: second}
"""


def gated(capability: str, gate: asyncio.Event) -> AgentDefinition:
    """An agent that finishes once ``gate`` is set."""

    @agent(f"{capability}-agent", capability=capability)
    async def work(ctx: Context, task: Any) -> dict[str, str]:
        await gate.wait()
        return {"done": capability}

    return definition_of(work)


def _open() -> asyncio.Event:
    gate = asyncio.Event()
    gate.set()
    return gate


def _requested(events: list[Event], capability: str) -> int:
    requested = [e for e in events if e.type == "task.requested"]
    return sum(_data(e)["capability"] == capability for e in requested)


async def _paused_in_first(harness: Any, gate: asyncio.Event) -> tuple[Any, str]:
    h = await harness(TWO_STEPS, gated("first", gate), gated("second", _open()))
    run_id = await h.start_run()
    await h.claimed(run_id, "first")
    await h.command(run_id, "run.pause.requested")
    await h.wait(run_id, "run.paused")
    return h, run_id


async def test_a_paused_run_starts_nothing_until_it_is_resumed(harness: Any) -> None:
    gate = asyncio.Event()
    h, run_id = await _paused_in_first(harness, gate)
    gate.set()
    events = await h.settled(run_id, "task.completed")
    assert _requested(events, "second") == 0
    state = await h.engine.load(TENANT, APP, run_id)
    assert (state.status, state.held, state.waiting) == ("paused", True, {"request"})
    assert state.steps["first"].status == "running"  # its result waits in the log

    await h.command(run_id, "run.resume.requested")
    events = await h.wait(run_id)
    assert _types(events, "run.") == [
        "run.started",
        "run.pause.requested",
        "run.paused",
        "run.resume.requested",
        "run.resumed",
        "run.completed",
    ]
    assert [_data(e)["step"] for e in events if e.type == "step.completed"] == ["first", "second"]


async def test_a_result_handled_after_the_pause_is_not_lost(harness: Any) -> None:
    gate = asyncio.Event()
    h = await harness(TWO_STEPS, gated("first", gate), gated("second", _open()))
    run_id = await h.start_run()
    await h.claimed(run_id, "first")
    await h.engine.kill()
    gate.set()
    await h.wait(run_id, "task.completed")
    await h.command(run_id, "run.pause.requested")
    events = await h.events(run_id)
    result = next(e for e in events if e.type == "task.completed")
    pause = next(e for e in events if e.type == "run.pause.requested")

    # The log says result, then pause; each has its own consumer group, so they can reach the
    # engine the other way round. This is the order that would strand a run on a simple rule.
    await h.engine._handle(pause)
    await h.engine._handle(result)
    await h.command(run_id, "run.resume.requested")
    await h.restart_engine()
    events = await h.wait(run_id)
    assert events[-1].type == "run.completed"
    assert _requested(events, "second") == 1


async def test_fan_out_items_wait_out_a_hold(harness: Any) -> None:
    gate = asyncio.Event()
    slow = replace(gated("research.web", gate), limits=Limits(concurrency=5))
    h = await harness(RESEARCH, definition_of(planner), slow, *_agents(reviewer([]))[2:])
    run_id = await h.start_run()
    await h.claimed(run_id, "research.web")
    await h.command(run_id, "run.pause.requested")
    await h.wait(run_id, "run.paused")
    gate.set()
    events = await h.settled(run_id, "task.completed", 3)  # the planner, then both requested items
    assert _requested(events, "research.web") == 2  # maxParallel; the third item stays queued

    await h.command(run_id, "run.resume.requested")
    events = await h.wait(run_id)
    assert events[-1].type == "run.completed"
    assert _requested(events, "research.web") == 3


async def test_a_retry_waits_out_a_hold(harness: Any) -> None:
    gate = asyncio.Event()
    attempts: list[int] = []

    @agent("flaky", capability="first")
    async def flaky(ctx: Context, task: Any) -> dict[str, bool]:
        attempts.append(ctx.attempt)
        await gate.wait()
        if ctx.attempt == 1:
            raise RuntimeError("try again")
        return {"done": True}

    h = await harness(TWO_STEPS, definition_of(flaky), gated("second", _open()))
    run_id = await h.start_run()
    await h.claimed(run_id, "first")
    await h.command(run_id, "run.pause.requested")
    await h.wait(run_id, "run.paused")
    gate.set()
    await h.settled(run_id, "task.failed")
    assert attempts == [1]

    await h.command(run_id, "run.resume.requested")
    events = await h.wait(run_id)
    assert events[-1].type == "run.completed"
    assert attempts == [1, 2]
    assert "step.retrying" in _types(events)


async def test_a_loop_back_waits_out_a_hold(harness: Any) -> None:
    gate = asyncio.Event()
    decisions = ["reject"]

    @agent("gated-reviewer", capability="review")
    async def review(ctx: Context, task: Any) -> dict[str, str]:
        await gate.wait()
        return {"result": decisions.pop(0) if decisions else "approve"}

    workflow = """
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: loop, version: 1.0.0}
spec:
  steps:
    - {id: draft, type: task, capability: writing}
    - {id: review, type: task, capability: review, onResult: {reject: draft}, maxLoops: 1}
"""
    h = await harness(workflow, definition_of(writer), definition_of(review))
    run_id = await h.start_run()
    await h.claimed(run_id, "review")
    await h.command(run_id, "run.pause.requested")
    await h.wait(run_id, "run.paused")
    gate.set()
    events = await h.settled(run_id, "task.completed", 2)
    assert _requested(events, "writing") == 1

    await h.command(run_id, "run.resume.requested")
    events = await h.wait(run_id)
    assert events[-1].type == "run.completed"
    assert _requested(events, "writing") == 2
    second_draft = [e for e in events if e.type == "task.requested"][2]
    assert _data(second_draft)["input"]["feedback"] == {"result": "reject"}


async def test_time_held_does_not_count_against_timeouts(harness: Any) -> None:
    gate = asyncio.Event()
    workflow = """
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: timed, version: 1.0.0}
spec:
  budget: {maxDuration: 500ms}
  steps:
    - {id: first, type: task, capability: first, timeout: 500ms}
"""
    h = await harness(workflow, gated("first", gate))
    run_id = await h.start_run()
    await h.claimed(run_id, "first")
    await h.command(run_id, "run.pause.requested")
    await h.wait(run_id, "run.paused")
    await asyncio.sleep(0.7)
    assert "run.failed" not in _types(await h.events(run_id))

    await h.command(run_id, "run.resume.requested")
    gate.set()
    events = await h.wait(run_id)
    assert events[-1].type == "run.completed"


async def test_a_timeout_still_fires_after_a_hold(harness: Any) -> None:
    gate = asyncio.Event()
    workflow = """
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: timed, version: 1.0.0}
spec:
  steps:
    - {id: first, type: task, capability: first, timeout: 300ms}
"""
    h = await harness(workflow, gated("first", gate))
    run_id = await h.start_run()
    await h.claimed(run_id, "first")
    await h.command(run_id, "run.pause.requested")
    await h.wait(run_id, "run.paused")
    await asyncio.sleep(0.4)
    await h.command(run_id, "run.resume.requested")
    events = await h.wait(run_id)
    gate.set()
    assert events[-1].type == "run.failed"
    assert _data(events[-1])["error"]["code"] == "step.timeout"


async def test_a_paused_run_can_be_cancelled(harness: Any) -> None:
    gate = asyncio.Event()
    h, run_id = await _paused_in_first(harness, gate)
    await h.command(run_id, "run.cancel.requested", reason="no longer needed")
    events = await h.wait(run_id)
    gate.set()
    assert events[-1].type == "run.cancelled"


async def test_a_hold_survives_an_engine_restart(harness: Any) -> None:
    gate = asyncio.Event()
    h, run_id = await _paused_in_first(harness, gate)
    gate.set()
    await h.settled(run_id, "task.completed")
    await h.restart_engine()
    await asyncio.sleep(0.05)
    assert _requested(await h.events(run_id), "second") == 0

    await h.command(run_id, "run.resume.requested")
    events = await h.wait(run_id)
    assert events[-1].type == "run.completed"
    assert [_data(e)["step"] for e in events if e.type == "step.completed"] == ["first", "second"]


async def test_repeated_and_redelivered_commands_change_nothing(harness: Any) -> None:
    gate = asyncio.Event()
    h, run_id = await _paused_in_first(harness, gate)
    await h.command(run_id, "run.pause.requested")
    await h.settled(run_id, "run.pause.requested", 2)
    assert _types(await h.events(run_id), "run.paused") == ["run.paused"]

    await h.command(run_id, "run.resume.requested")
    events = await h.settled(run_id, "run.resumed")
    resume = next(e for e in events if e.type == "run.resume.requested")
    await h.engine._handle(resume)
    assert _types(await h.events(run_id), "run.resumed") == ["run.resumed"]
    gate.set()
    await h.wait(run_id)
    await h.command(run_id, "run.resume.requested")  # the run has finished
    await h.settled(run_id, "run.resume.requested", 2)
    assert _types(await h.events(run_id), "run.resumed") == ["run.resumed"]


async def test_an_approval_granted_during_a_hold_waits_for_the_resume(harness: Any) -> None:
    h = await harness(APPROVAL, definition_of(tool_runner))
    run_id = await h.start_run()
    events = await h.wait(run_id, "run.paused")
    request = next(e for e in events if e.type == "approval.requested")
    await h.command(run_id, "run.pause.requested")
    await h.settled(run_id, "run.paused", 2)
    state = await h.engine.load(TENANT, APP, run_id)
    assert state.waiting == {"approval:approve", "request"}

    await h.bus.publish(
        request.derive(
            "approval.granted",
            {"approval": _data(request)["approval"], "by": "user:eve@acme"},
            source=f"aios://{TENANT}/system/human",
            principal="user:eve@acme",
        )
    )
    events = await h.settled(run_id, "step.completed")
    assert "task.requested" not in _types(events)
    state = await h.engine.load(TENANT, APP, run_id)
    assert (state.status, state.waiting) == ("paused", {"request"})

    await h.command(run_id, "run.resume.requested")
    events = await h.wait(run_id)
    assert events[-1].type == "run.completed"


# --------------------------------------------------------------------------- signals

WAITING = """
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: waiting, version: 1.0.0}
spec:
  output: ${finish.output}
  steps:
    - {id: ask, type: task, capability: first}
    - {id: reply, type: wait, signal: customer-reply}
    - {id: finish, type: task, capability: second, input: {reply: "${reply.output}"}}
"""


@agent("echo-agent", capability="second")
async def echo(ctx: Context, task: Any) -> Any:
    return task


async def _at_the_wait(harness: Any, workflow: str = WAITING) -> tuple[Any, str]:
    h = await harness(workflow, gated("first", _open()), definition_of(echo))
    run_id = await h.start_run()
    await h.wait(run_id, "run.paused")
    return h, run_id


async def test_a_wait_step_pauses_until_its_signal_arrives(harness: Any) -> None:
    h, run_id = await _at_the_wait(harness)
    state = await h.engine.load(TENANT, APP, run_id)
    assert (state.status, state.waiting) == ("paused", {"signal:reply"})

    await h.command(run_id, "run.signal.requested", name="something-else", data=1)
    events = await h.settled(run_id, "run.signal.requested")
    assert "run.resumed" not in _types(events)

    await h.command(run_id, "run.signal.requested", name="customer-reply", data={"ok": True})
    events = await h.wait(run_id)
    assert events[-1].type == "run.completed"
    assert _data(events[-1])["output"] == {"reply": {"ok": True}}
    sent = [e for e in events if e.type == "run.signal.requested"][-1]
    done = next(e for e in events if e.type == "step.completed" and _data(e)["step"] == "reply")
    assert _data(done)["signal"] == sent.id
    assert done.causationid == sent.id


async def test_a_signal_sent_early_is_kept_for_the_step(harness: Any) -> None:
    gate = asyncio.Event()
    h = await harness(WAITING, gated("first", gate), definition_of(echo))
    run_id = await h.start_run()
    await h.claimed(run_id, "first")
    await h.command(run_id, "run.signal.requested", name="customer-reply", data="early")
    await h.settled(run_id, "run.signal.requested")
    gate.set()
    events = await h.wait(run_id)
    assert _data(events[-1])["output"] == {"reply": "early"}


async def test_each_wait_step_takes_its_own_signal(harness: Any) -> None:
    workflow = """
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: twice, version: 1.0.0}
spec:
  output: ${second.output}
  steps:
    - {id: first, type: wait, signal: answer}
    - {id: second, type: wait, signal: answer}
"""
    h = await harness(workflow)
    run_id = await h.start_run()
    await h.command(run_id, "run.signal.requested", name="answer", data="one")
    await h.command(run_id, "run.signal.requested", name="answer", data="two")
    events = await h.wait(run_id)
    assert _data(events[-1])["output"] == "two"


async def test_a_wait_can_time_out(harness: Any) -> None:
    workflow = """
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: waiting, version: 1.0.0}
spec:
  steps:
    - {id: reply, type: wait, signal: customer-reply, timeout: 100ms}
"""
    h = await harness(workflow)
    events = await h.wait(await h.start_run())
    assert _data(events[-1])["error"]["code"] == "step.timeout"


async def test_a_signal_during_a_hold_waits_for_the_resume(harness: Any) -> None:
    h, run_id = await _at_the_wait(harness)
    await h.command(run_id, "run.pause.requested")
    await h.settled(run_id, "run.paused", 2)
    await h.command(run_id, "run.signal.requested", name="customer-reply", data="held")
    events = await h.settled(run_id, "run.signal.requested")
    assert "run.resumed" not in _types(events)

    await h.command(run_id, "run.resume.requested")
    events = await h.wait(run_id)
    assert _data(events[-1])["output"] == {"reply": "held"}


async def test_a_signal_reaches_a_run_across_an_engine_restart(harness: Any) -> None:
    h, run_id = await _at_the_wait(harness)
    await h.engine.kill()
    await h.command(run_id, "run.signal.requested", name="customer-reply", data="later")
    await h.restart_engine()
    events = await h.wait(run_id)
    assert _data(events[-1])["output"] == {"reply": "later"}
    assert _types(events, "run.resumed") == ["run.resumed"]


def test_the_dsl_compiles_wait_steps() -> None:
    compiled = (
        Workflow("waiting", version="1.0.0")
        .then(Step("ask", capability="first"))
        .then(Wait("reply", signal="customer-reply", timeout="1h"))
        .compile()
    )
    step = compiled.spec.steps[1]
    assert step.model_dump(exclude_defaults=True, exclude={"timeout"}) == {
        "id": "reply",
        "type": "wait",
        "signal": "customer-reply",
    }


# --------------------------------------------------------------------------- suspended tasks

SUSPENDED = """
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: suspended, version: 1.0.0}
spec:
  steps:
    - {id: refund, type: task, capability: unserved}
"""
APPROVAL_ID = "01J0000000000000000000000A"


def _suspended(task: Event) -> Event:
    return task.derive(
        "task.suspended",
        {"capability": "unserved", "approval": APPROVAL_ID},
        source=f"aios://{TENANT}/apps/{APP}/agents/a",
        principal=f"agent:a@{TENANT}",
    )


def _granted(task: Event) -> Event:
    return task.derive(
        "approval.granted",
        {"approval": APPROVAL_ID, "by": "user:eve@acme"},
        source=f"aios://{TENANT}/system/human",
        principal="user:eve@acme",
    )


async def _tasks(h: Any, run_id: str, count: int) -> list[Event]:
    async with asyncio.timeout(5):
        while True:
            found = [e for e in await h.events(run_id) if e.type == "task.requested"]
            if len(found) >= count:
                return found
            await asyncio.sleep(0.01)


async def test_a_suspended_task_is_requested_again_with_its_input(harness: Any) -> None:
    h = await harness(SUSPENDED)
    run_id = await h.start_run()
    [task] = await _tasks(h, run_id, 1)
    await h.bus.publish(_suspended(task))
    await h.wait(run_id, "run.paused")
    await h.bus.publish(_granted(task))
    [_, again] = await _tasks(h, run_id, 2)
    assert _data(again)["input"] == _data(task)["input"]
    assert _data(again)["capability"] == "unserved"
    assert (_data(again)["step"], _data(again)["attempt"]) == ("refund", _data(task)["attempt"])
    assert _data(again)["resumes"] == [task.id]
    state = await h.engine.load(TENANT, APP, run_id)
    assert (state.status, state.waiting) == ("running", set[str]())


async def test_an_answer_that_overtakes_the_suspension_isnt_lost(harness: Any) -> None:
    """The two come through separate consumer groups, so either can be seen first."""
    h = await harness(SUSPENDED)
    run_id = await h.start_run()
    [task] = await _tasks(h, run_id, 1)
    await h.bus.publish(_granted(task))
    await asyncio.sleep(0.05)
    await h.bus.publish(_suspended(task))
    [_, again] = await _tasks(h, run_id, 2)
    assert _data(again)["resumes"] == [task.id]
    assert _types(await h.events(run_id), "run.") == ["run.started", "run.paused", "run.resumed"]


async def test_a_redelivered_answer_doesnt_resume_twice(harness: Any) -> None:
    h = await harness(SUSPENDED)
    run_id = await h.start_run()
    [task] = await _tasks(h, run_id, 1)
    await h.bus.publish(_suspended(task))
    await h.wait(run_id, "run.paused")
    answer = _granted(task)
    await h.bus.publish(answer)
    await _tasks(h, run_id, 2)
    await h.bus.publish(answer)
    await asyncio.sleep(0.05)
    assert len([e for e in await h.events(run_id) if e.type == "task.requested"]) == 2


async def test_a_stale_suspension_is_ignored(harness: Any) -> None:
    h = await harness(SUSPENDED)
    run_id = await h.start_run()
    [task] = await _tasks(h, run_id, 1)
    await h.bus.publish(_suspended(task))
    await h.wait(run_id, "run.paused")
    await h.bus.publish(_granted(task))
    await _tasks(h, run_id, 2)
    await h.bus.publish(_suspended(task))  # the first execution's, after the second has started
    await asyncio.sleep(0.05)
    state = await h.engine.load(TENANT, APP, run_id)
    assert state.status == "running"
