import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from typing import Any

import pytest
from pydantic import BaseModel, JsonValue

from aios.core.catalog import Usage
from aios.core.envelope import Event
from aios.core.manifests import Limits
from aios.core.ports import (
    CallSite,
    ModelGateway,
    ModelRequest,
    ModelResponse,
    ToolGateway,
    ToolResult,
)
from aios.fabric import InMemoryEventBus, Scope
from aios.kernel import AgentDefinition, Context, Installation, Worker, agent, definition_of
from aios.testing import make_event, task_request

SCOPE = Scope("acme", "ai-researcher")


class FakeModels(ModelGateway):
    def __init__(self, text: str = '{"answer": "42"}') -> None:
        self.text = text
        self.sites: list[CallSite] = []

    async def generate(self, request: ModelRequest, site: CallSite) -> ModelResponse:
        self.sites.append(site)
        usage = Usage(input_tokens=10, output_tokens=5, cost_usd=0.01)
        return ModelResponse(text=self.text, provider="fake", model="m", usage=usage)


class FakeTools(ToolGateway):
    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, JsonValue],
        site: CallSite,
        *,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        return ToolResult(tool=name, output={"echo": dict(arguments)}, provider="fake")


class Answer(BaseModel):
    answer: str


class Question(BaseModel):
    question: str


@agent("answer-agent", capability="answering")
async def answer(ctx: Context, task: Question) -> Answer:
    hits = await ctx.tools.invoke("search.web.query", q=task.question)
    assert ctx.tainted  # tool output is untrusted
    result = await ctx.model.generate(messages=f"{task.question} {hits}", output=Answer)
    await ctx.emit("answer.found", result)
    return result


@agent("broken-agent", capability="breaking")
async def broken(ctx: Context, task: Any) -> None:
    raise RuntimeError("boom")


def _definition(handler: Any, **changes: Any) -> AgentDefinition:
    return replace(definition_of(handler), **changes)


async def _worker(
    bus: InMemoryEventBus, *definitions: AgentDefinition, models: ModelGateway | None = None
) -> Worker:
    worker = Worker(
        bus,
        models=models or FakeModels(),
        tools=FakeTools(),
        installations=[Installation("acme", "ai-researcher", definitions)],
        lease=0.5,
    )
    await worker.start()
    return worker


@pytest.fixture
async def bus() -> AsyncIterator[InMemoryEventBus]:
    async with InMemoryEventBus() as bus:
        yield bus


def _error(event: Event) -> dict[str, Any]:
    assert isinstance(event.data, dict)
    error = event.data["error"]
    assert isinstance(error, dict)
    return error


async def _until(bus: InMemoryEventBus, run_id: str, type: str, count: int = 1) -> list[Event]:
    for _ in range(200):
        events = [s.event async for s in bus.replay(SCOPE, run_id=run_id)]
        if sum(e.type == type for e in events) >= count:
            return events
        await asyncio.sleep(0.01)
    raise AssertionError(f"no {type} for run {run_id}")


async def test_dispatches_a_task_and_publishes_its_result(bus: InMemoryEventBus) -> None:
    worker = await _worker(bus, definition_of(answer))
    request = task_request("answering", {"question": "why?"})
    await bus.publish(request)
    events = await _until(bus, str(request.runid), "task.completed")
    types = [e.type for e in events]
    assert types == ["task.requested", "task.claimed", "answer.found", "task.completed"]
    completed = events[-1]
    assert completed.causationid == request.id
    assert completed.principal == "agent:answer-agent@acme"
    assert isinstance(completed.data, dict)
    assert completed.data["output"] == {"answer": "42"}
    assert completed.data["usage"] == {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01}
    await worker.stop()


@pytest.mark.parametrize(
    ("handler", "task_input", "code"),
    [(broken, None, "agent.error"), (answer, {"wrong": 1}, "task.invalid_input")],
)
async def test_agent_failures_become_task_failed(
    bus: InMemoryEventBus, handler: Any, task_input: Any, code: str
) -> None:
    definition = definition_of(handler)
    worker = await _worker(bus, definition)
    request = task_request(definition.capabilities[0], task_input)
    await bus.publish(request)
    events = await _until(bus, str(request.runid), "task.failed")
    assert _error(events[-1])["code"] == code
    await worker.stop()


async def test_step_budgets_stop_the_agent(bus: InMemoryEventBus) -> None:
    worker = await _worker(bus, _definition(answer, limits=Limits(max_steps=1)))
    request = task_request("answering", {"question": "why?"})
    await bus.publish(request)
    events = await _until(bus, str(request.runid), "task.failed")
    assert [e.type for e in events][-2:] == ["budget.exceeded", "task.failed"]
    assert events[-2].data == {"budget": "steps", "limit": 1.0, "observed": 2.0}
    await worker.stop()


async def test_model_output_is_validated_against_the_output_type(bus: InMemoryEventBus) -> None:
    worker = await _worker(bus, definition_of(answer), models=FakeModels("not json"))
    request = task_request("answering", {"question": "why?"})
    await bus.publish(request)
    events = await _until(bus, str(request.runid), "task.failed")
    assert _error(events[-1])["code"] == "model.invalid_output"
    await worker.stop()


async def test_a_task_already_settled_is_not_run_again(bus: InMemoryEventBus) -> None:
    models = FakeModels()
    request = task_request("answering", {"question": "why?"})
    done = request.derive(
        "task.completed",
        {"capability": "answering", "output": {"answer": "old"}},
        source="aios://acme/apps/ai-researcher/agents/answer-agent",
        principal="agent:answer-agent@acme",
    )
    worker = await _worker(bus, definition_of(answer), models=models)
    await bus.publish(request, done)  # e.g. a crash after publishing the result, before the ack
    await asyncio.sleep(0.1)
    assert models.sites == []
    await worker.stop()


@agent("ping-agent", subscribe="ping.sent")
async def ping(ctx: Context, event: Event) -> None:
    await ctx.emit("pong.sent")


@agent("pong-agent", subscribe="pong.sent")
async def pong(ctx: Context, event: Event) -> None:
    await ctx.emit("ping.sent")


async def test_mutually_triggering_agents_stop_at_the_hop_limit(bus: InMemoryEventBus) -> None:
    worker = await _worker(bus, definition_of(ping), definition_of(pong))
    stream = await bus.subscribe(SCOPE, ">")
    await bus.publish(make_event("ping.sent"))
    seen: list[Event] = []
    while not seen or seen[-1].type != "budget.exceeded":
        delivery = await stream.next(timeout=5)
        assert delivery is not None
        await delivery.ack()
        if not delivery.event.type.startswith("agent."):
            seen.append(delivery.event)
    assert max(e.hopcount for e in seen) == bus.max_hops
    assert seen[-1].hopcount == bus.max_hops
    assert seen[-1].data == {"budget": "hops", "limit": 64.0, "observed": 65.0}
    await worker.stop()


async def test_lifecycle_events(bus: InMemoryEventBus) -> None:
    stream = await bus.subscribe(SCOPE, "agent.lifecycle.*")
    worker = await _worker(bus, definition_of(answer))
    await worker.stop()
    states: list[str] = []
    while (delivery := await stream.next(timeout=0.1)) is not None:
        await delivery.ack()
        states.append(delivery.event.type.rsplit(".", 1)[1])
    assert states == ["registered", "starting", "ready", "draining", "stopped"]


async def test_tasks_run_in_parallel_up_to_the_agent_concurrency(bus: InMemoryEventBus) -> None:
    running = 0
    peak = 0

    @agent("slow-agent", capability="slow")
    async def slow(ctx: Context, task: Any) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1

    worker = await _worker(bus, _definition(slow, limits=Limits(concurrency=3)))
    requests = [task_request("slow") for _ in range(6)]
    await bus.publish(*requests)
    for request in requests:
        await _until(bus, str(request.runid), "task.completed")
    assert peak == 3
    await worker.stop()


async def test_a_killed_worker_loses_nothing(bus: InMemoryEventBus) -> None:
    started = asyncio.Event()

    @agent("stuck-agent", capability="stuck")
    async def stuck(ctx: Context, task: Any) -> str:
        if not started.is_set():
            started.set()
            await asyncio.sleep(60)  # killed here
        return "done"

    definition = definition_of(stuck)
    first = await _worker(bus, definition)
    request = task_request("stuck")
    await bus.publish(request)
    await asyncio.wait_for(started.wait(), 2)
    await first.kill()
    second = await _worker(bus, definition)
    events = await _until(bus, str(request.runid), "task.completed")
    claims = [e for e in events if e.type == "task.claimed"]
    assert len(claims) == 2
    assert isinstance(events[-1].data, dict)
    assert events[-1].data["output"] == "done"
    await second.stop()


def test_handlers_must_be_async_and_take_ctx_and_input() -> None:
    def sync(ctx: Context, task: Any) -> None: ...

    async def one_argument(ctx: Context) -> None: ...

    with pytest.raises(TypeError, match="async"):
        agent("x", capability="x")(sync)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match=r"\(ctx, input\)"):
        agent("x", capability="x")(one_argument)
