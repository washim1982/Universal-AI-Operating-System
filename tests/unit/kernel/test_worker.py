import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from typing import Any, cast

import pytest
from pydantic import BaseModel, JsonValue

from aios.core.catalog import Usage
from aios.core.envelope import Event
from aios.core.manifests import Limits
from aios.core.ports import (
    CallSite,
    EmbeddingRequest,
    EmbeddingResponse,
    ModelGateway,
    ModelRequest,
    ModelResponse,
    NoMemory,
    ToolGateway,
    ToolResult,
)
from aios.fabric import InMemoryEventBus, Scope
from aios.kernel import AgentDefinition, Context, Installation, Worker, agent, definition_of
from aios.kernel.context import MAX_TAINT_SOURCES
from aios.testing import make_event, task_request

SCOPE = Scope("acme", "ai-researcher")


class FakeModels(ModelGateway):
    def __init__(self, text: str = '{"answer": "42"}') -> None:
        self.text = text
        self.sites: list[CallSite] = []
        self.embedded: list[EmbeddingRequest] = []

    async def generate(self, request: ModelRequest, site: CallSite) -> ModelResponse:
        self.sites.append(site)
        usage = Usage(input_tokens=10, output_tokens=5, cost_usd=0.01)
        return ModelResponse(text=self.text, provider="fake", model="m", usage=usage)

    async def embed(self, request: EmbeddingRequest, site: CallSite) -> EmbeddingResponse:
        self.sites.append(site)
        self.embedded.append(request)
        usage = Usage(input_tokens=4, cost_usd=0.002)
        vectors = [[float(len(text))] for text in request.texts]
        return EmbeddingResponse(vectors=vectors, provider="fake", model="e", usage=usage)


class FakeTools(ToolGateway):
    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, JsonValue],
        site: CallSite,
        *,
        idempotency_key: str | None = None,
        rationale: str | None = None,
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
    assert completed.data["usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": 0.01,
    }
    await worker.stop()


async def test_agents_embed_within_their_budget(bus: InMemoryEventBus) -> None:
    @agent("embedding-agent", capability="embedding")
    async def embedding(ctx: Context, task: Any) -> dict[str, Any]:
        return {"one": await ctx.model.embed("abc"), "many": await ctx.model.embed(["a", "bb"])}

    models = FakeModels()
    worker = await _worker(bus, definition_of(embedding), models=models)
    request = task_request("embedding", dataclass="confidential")
    await bus.publish(request)
    events = await _until(bus, str(request.runid), "task.completed")
    data = events[-1].data
    assert isinstance(data, dict)
    assert data["output"] == {"one": [[3.0]], "many": [[1.0], [2.0]]}
    usage = Usage.model_validate(data["usage"])
    assert (usage.input_tokens, usage.cost_usd) == (8, 0.004)
    assert [site.index for site in models.sites] == [0, 1]
    assert models.embedded[0].data_class == request.dataclass
    await worker.stop()


async def test_a_task_that_outlives_its_lease_runs_once(bus: InMemoryEventBus) -> None:
    runs = 0

    @agent("slow-agent", capability="slowness")
    async def slow(ctx: Context, task: Any) -> None:
        nonlocal runs
        runs += 1
        await asyncio.sleep(1.2)  # a live model call, well past the 0.5 s lease

    worker = await _worker(bus, _definition(slow, limits=Limits(concurrency=5)))
    request = task_request("slowness")
    await bus.publish(request)
    events = await _until(bus, str(request.runid), "task.completed")
    assert runs == 1
    assert [e.type for e in events].count("task.claimed") == 1
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
    assert events[-2].data == {"budget": "steps", "limit": 1.0, "observed": 2.0, "level": None}
    await worker.stop()


async def test_model_output_is_validated_against_the_output_type(bus: InMemoryEventBus) -> None:
    worker = await _worker(bus, definition_of(answer), models=FakeModels("not json"))
    request = task_request("answering", {"question": "why?"})
    await bus.publish(request)
    events = await _until(bus, str(request.runid), "task.failed")
    assert _error(events[-1])["code"] == "model.invalid_output"
    await worker.stop()


@pytest.mark.parametrize(
    ("settled", "data"),
    [
        ("task.completed", {"output": {"answer": "old"}}),
        ("task.suspended", {"approval": "01J0000000000000000000000A"}),
    ],
)
async def test_a_task_already_settled_is_not_run_again(
    bus: InMemoryEventBus, settled: str, data: dict[str, Any]
) -> None:
    """A suspended one least of all: its answer may be in by now, and a new execution has it."""
    models = FakeModels()
    request = task_request("answering", {"question": "why?"})
    done = request.derive(
        settled,
        {"capability": "answering", **data},
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
    assert seen[-1].data == {"budget": "hops", "limit": 64.0, "observed": 65.0, "level": None}
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


# --------------------------------------------------------------------------- shared slots


async def test_an_agent_serves_all_it_listens_to_with_one_slot(bus: InMemoryEventBus) -> None:
    """One subscription waiting for work must not starve the others."""
    heard: list[str] = []

    @agent("busy-agent", capability=("first.job", "second.job"), subscribe="order.item.noted")
    async def handler(ctx: Context, payload: Any) -> None:
        heard.append(ctx.event.type if ctx.step is None else str(ctx.step))

    worker = await _worker(bus, definition_of(handler))
    await bus.publish(task_request("second.job", None, step="second"))
    await bus.publish(make_event("order.item.noted", {}))
    await bus.publish(task_request("first.job", None, step="first"))
    async with asyncio.timeout(5):
        while True:
            if len(heard) == 3:
                break
            await asyncio.sleep(0.01)
    await worker.stop()
    assert sorted(heard) == ["first", "order.item.noted", "second"]


# --------------------------------------------------------------------------- running again


RUN = "01J0000000000000000000000A"


def _context(task: Event, bus: InMemoryEventBus) -> Context:
    @agent("again-agent", capability="work")
    async def handler(ctx: Context, payload: Any) -> None: ...

    return Context(
        event=task,
        definition=definition_of(handler),
        bus=bus,
        models=FakeModels(),
        tools=FakeTools(),
        memory=NoMemory(),
        instance="i",
        logger=logging.getLogger("test"),
    )


def _step_task(*, run: str = RUN, **data: Any) -> Event:
    task = task_request("work", None, step="s", run_id=run)
    return task.model_copy(update={"data": {**cast(dict[str, Any], task.data), **data}})


def test_every_attempt_of_a_step_shares_its_idempotency_key() -> None:
    """A retry after a timeout must not refund again if the provider did the first one."""
    bus = InMemoryEventBus()
    first = _context(_step_task(attempt=1), bus).step_key("refund")
    second = _context(_step_task(attempt=2), bus).step_key("refund")
    assert first == second


def test_fan_out_items_and_loops_have_their_own_idempotency_keys() -> None:
    bus = InMemoryEventBus()
    keys = {
        _context(_step_task(item=0), bus).step_key("refund"),
        _context(_step_task(item=1), bus).step_key("refund"),
        _context(_step_task(item=0, loop=1), bus).step_key("refund"),
        _context(_step_task(item=0), bus).step_key("other"),
    }
    assert len(keys) == 4


def test_a_task_keeps_a_bounded_list_of_where_things_came_from() -> None:
    """A task that reads a thousand pages doesn't get a thousand sources in every event."""
    ctx = _context(_step_task(), InMemoryEventBus())
    for number in range(100):
        ctx.taint(f"tool:{number}")
    ctx.taint("tool:0")
    assert ctx.tainted
    assert len(ctx.taint_sources) == MAX_TAINT_SOURCES
    assert len(set(ctx.taint_sources)) == MAX_TAINT_SOURCES


async def test_a_redelivered_task_states_a_fact_once() -> None:
    async with InMemoryEventBus() as bus:
        task = _step_task()
        first = await _context(task, bus).emit("order.item.noted", {"n": 1})
        again = await _context(task, bus).emit("order.item.noted", {"n": 1})
        assert again.id == first.id
        assert len([s async for s in bus.replay(Scope("*"), "order.item.noted")]) == 1


async def test_a_resumed_task_states_a_fact_once() -> None:
    async with InMemoryEventBus() as bus:
        task = _step_task()
        first = await _context(task, bus).emit("order.item.noted")
        again = await _context(_step_task(resumes=[task.id]), bus).emit("order.item.noted")
        assert again.id == first.id


async def test_two_facts_of_one_task_are_two_facts() -> None:
    async with InMemoryEventBus() as bus:
        ctx = _context(_step_task(), bus)
        await ctx.emit("order.item.noted")
        await ctx.emit("order.item.noted")
        assert len([s async for s in bus.replay(Scope("*"), "order.item.noted")]) == 2


async def test_a_retry_states_its_facts_again() -> None:
    """A retry is a new attempt, not a replay: the first attempt's facts stand as they were."""
    async with InMemoryEventBus() as bus:
        await _context(_step_task(attempt=1), bus).emit("order.item.noted")
        await _context(_step_task(attempt=2), bus).emit("order.item.noted")
        assert len([s async for s in bus.replay(Scope("*"), "order.item.noted")]) == 2
