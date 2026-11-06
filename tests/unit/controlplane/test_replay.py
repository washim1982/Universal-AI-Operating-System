"""Replay, fork and retry (ADR-021; FR-OBS-04, FR-WF-11): what each keeps, holds back and redoes."""

import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from aios.controlplane import Runtime, views
from aios.core.envelope import Event
from aios.core.errors import ConflictError, ModelError, NotFoundError, ToolError
from aios.core.ids import new_ulid
from aios.core.manifests import load_manifests
from aios.core.policy import SideEffect
from aios.core.ports import CallSite, EmbeddingRequest, ModelRequest
from aios.fabric import Scope, StoredEvent
from aios.kernel import Context
from aios.models import Completion, ModelEntry, Provider, ProviderError
from aios.testing import make_event, task_request
from aios.testing.runtime import LocalRuntime, RunHandle, RunResult
from aios.tools import NativeProvider, tool
from aios.workflow import fold

ROOT = Path(__file__).resolve().parents[3]
RESEARCHER = ROOT / "apps" / "ai-researcher"
CASSETTES = ROOT / "tests" / "cassettes" / "ai-researcher"
QUESTION = "What changed in the OpenTelemetry GenAI semantic conventions in 2026?"
SHOP = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: shop, version: 1.0.0}
spec:
  entrypoints: [{type: chat, workflow: order}]
  agents: [clerk]
  workflows: [order]
  memory: {scopes: [app]}
---
apiVersion: aios/v1alpha1
kind: Agent
metadata: {name: clerk, version: 1.0.0}
spec:
  runtime: {type: python, entrypoint: "replay_shop:work"}
  capabilities: [{name: shop.check}, {name: shop.pay}, {name: shop.tell}]
  tools: {allow: ["shop.>"]}
  memory: {read: [app], write: [app]}
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: order, version: 1.0.0}
spec:
  steps:
    - id: check
      type: task
      capability: shop.check
      input: {do: check, item: "${input.item}", reader: "${input.reader}"}
    - {id: go, type: wait, signal: go}
    - {id: pay, type: task, capability: shop.pay, input: {do: pay, amount: "${input.amount}"}}
    - {id: tell, type: task, capability: shop.tell, input: {do: tell}}
"""
DENY = """
---
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: no-orders, version: 1.0.0}
spec:
  subject: {app: shop}
  deny: [{action: tool.invoke, resource: shop.order.place}]
"""
ASK = """
---
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: big-orders, version: 1.0.0}
spec:
  subject: {app: shop}
  requireApproval:
    - action: tool.invoke
      resource: shop.order.place
      when: {input: {amount: {gt: 100}}}
      approvers: ["role:lead"]
"""
ORDER = {"item": "apple", "amount": 50, "reader": "shop.stock.read"}
NAMER = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: namer, version: 1.0.0}
spec:
  entrypoints: [{type: chat, workflow: name}]
  agents: [namer]
  workflows: [name]
  modelPolicy: only-fake
---
apiVersion: aios/v1alpha1
kind: Agent
metadata: {name: namer, version: 1.0.0}
spec:
  runtime: {type: python, entrypoint: "replay_namer:work"}
  capabilities: [{name: namer.name}]
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: name, version: 1.0.0}
spec:
  steps:
    - {id: name, type: task, capability: namer.name}
---
apiVersion: aios/v1alpha1
kind: ModelPolicy
metadata: {name: only-fake, version: 1.0.0}
spec:
  default: {candidates: [fake/one]}
"""


@dataclass
class Shop:
    runtime: LocalRuntime
    placed: list[Any] = field(default_factory=list[Any])
    reads: list[str] = field(default_factory=list[str])
    till_down: bool = False

    @property
    def core(self) -> Runtime:
        return self.runtime.runtime

    @property
    def tenant(self) -> str:
        return self.core.tenant

    async def start(self, **changes: Any) -> RunHandle:
        run = await self.core.chat("shop", input=ORDER | changes)
        return RunHandle(self.runtime, "shop", run)

    async def order(self, **changes: Any) -> str:
        handle = await self.start(**changes)
        await handle.wait_for_event("run.paused")
        await self.core.signal("shop", handle.run_id, "go")
        await handle.wait()
        return handle.run_id

    async def restart(self, run: str, mode: Any, **options: Any) -> RunResult:
        new = await self.core.restart("shop", run, mode, **options)
        return await RunHandle(self.runtime, "shop", new).wait(timeout=10)

    async def events(self, run: str, pattern: str = ">") -> list[Event]:
        scope = Scope(self.tenant, "shop")
        return [s.event async for s in self.core.bus.replay(scope, pattern, run_id=run)]


async def _clerk(ctx: Context, task: dict[str, Any]) -> Any:
    if task["do"] == "check":
        reader = task.get("reader") or "shop.stock.read"
        return {"item": task["item"], "stock": await ctx.tools.invoke(reader, item=task["item"])}
    if task["do"] == "pay":  # its key has the run's id in it, as ctx.step_key's does
        key = ctx.step_key("pay")
        return await ctx.tools.invoke("shop.order.place", amount=task["amount"], key=key)
    await ctx.emit("shop.order.confirmed", {"ok": True})
    await ctx.memory.put("an order was placed", scope="app")
    return "told"


CLERK: dict[str, Callable[[Context, dict[str, Any]], Awaitable[Any]]] = {"now": _clerk}
"""The agent's code. A test swaps it to stand for an agent that has changed since a run."""


async def _work(ctx: Context, task: dict[str, Any]) -> Any:
    return await CLERK["now"](ctx, task)


OpenShop = Callable[..., Awaitable[Shop]]


@pytest.fixture
async def open_shop(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[OpenShop]:
    module = ModuleType("replay_shop")
    module.work = _work  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "replay_shop", module)
    running: list[LocalRuntime] = []

    async def make(policies: str = "") -> Shop:
        runtime = LocalRuntime()
        await runtime.__aenter__()
        running.append(runtime)
        shop = Shop(runtime)

        @tool("shop.stock.read", side_effect=SideEffect.READ, trusted=True)
        async def stock(item: str) -> int:
            shop.reads.append(item)
            return 7

        @tool("shop.review.read", side_effect=SideEffect.READ)
        async def review(item: str) -> str:
            shop.reads.append(item)
            return "five stars"

        @tool("shop.order.place", side_effect=SideEffect.WRITE, trusted=True)
        async def place(amount: float, key: str = "") -> dict[str, Any]:
            if shop.till_down:
                raise ToolError("the till is down")
            shop.placed.append(amount)
            return {"receipt": f"R{len(shop.placed)}", "amount": amount}

        @tool("shop.label.read", side_effect=SideEffect.READ, trusted=True)
        async def label(item: str) -> dict[str, int]:
            return {"zeta": 1, "alpha": 2}

        @tool("shop.mail.send", side_effect=SideEffect.WRITE)
        async def mail(to: str) -> str:
            return f"sent to {to}"

        await runtime.runtime.register_tool_provider(
            NativeProvider("shop", [stock, review, place, label, mail])
        )
        await runtime.runtime.apply(load_manifests(SHOP + policies))
        return shop

    yield make
    for runtime in running:
        await runtime.__aexit__(None, None, None)


@pytest.fixture
async def researched() -> AsyncIterator[tuple[LocalRuntime, str]]:
    # Durable: on PostgreSQL when AIOS_TEST_DATABASE_URL is set, whose jsonb reorders keys.
    async with LocalRuntime.from_app(RESEARCHER, cassettes=CASSETTES, durable=True) as runtime:
        handle = await runtime.chat("ai-researcher", QUESTION)
        await handle.wait(timeout=60)
        yield runtime, handle.run_id


async def _restart(
    runtime: LocalRuntime, run: str, mode: Any, **options: Any
) -> tuple[str, RunResult]:
    new = await runtime.runtime.restart("ai-researcher", run, mode, **options)
    return new, await RunHandle(runtime, "ai-researcher", new).wait(timeout=60)


# --------------------------------------------------------------------------- replay


async def test_a_replay_answers_everything_from_the_record(
    researched: tuple[LocalRuntime, str],
) -> None:
    runtime, run = researched
    _, result = await _restart(runtime, run, "replay")
    original = await RunHandle(runtime, "ai-researcher", run).wait()
    assert result.status == "completed"
    assert result.output == original.output
    calls = [e.data for e in result.events if e.type == "model.call.completed"]
    assert len(calls) == 7
    assert all(isinstance(c, dict) and c["replayed"] for c in calls)
    tools = [e.data for e in result.events if e.type == "tool.call.completed"]
    assert tools
    assert all(isinstance(c, dict) and c["replayed"] for c in tools)


async def test_a_replay_spends_nothing(researched: tuple[LocalRuntime, str]) -> None:
    runtime, run = researched
    ledger = runtime.runtime.models.ledger
    month = (await RunHandle(runtime, "ai-researcher", run).events())[0].time.strftime("%Y-%m")
    before = await ledger.spent(runtime.runtime.tenant, [f"tenant:{month}"])
    _, result = await _restart(runtime, run, "replay")
    assert result.usage.cost_usd == 0
    assert await ledger.spent(runtime.runtime.tenant, [f"tenant:{month}"]) == before


async def test_a_replay_says_where_it_came_from(researched: tuple[LocalRuntime, str]) -> None:
    runtime, run = researched
    new, _ = await _restart(runtime, run, "replay")
    [listed] = [r for r in await runtime.runtime.runs("ai-researcher") if r.run_id == new]
    assert (listed.source_run, listed.mode) == (run, "replay")
    drawn = await views.trace(runtime.runtime, runtime.runtime.tenant, "ai-researcher", new)
    assert drawn.source is not None
    assert drawn.source["run"] == run
    calls = [call for node in drawn.nodes for call in node.model_calls]
    assert calls
    assert all(call.replayed for call in calls)


def test_a_run_says_where_it_came_from_before_it_starts() -> None:
    """The console opens a fork as soon as it is asked for, before the engine has started it."""
    run = new_ulid()
    source = {"run": new_ulid(), "mode": "fork", "step": "pay", "copied": [{"step": "check"}]}
    asked = make_event("app.request.received", {"entrypoint": "fork", "source": source}, run_id=run)
    state = fold(run, [StoredEvent(position=1, event=asked)])
    assert state.started_event is None
    assert state.source == source


async def test_a_replay_and_its_source_compare_the_same(
    researched: tuple[LocalRuntime, str],
) -> None:
    runtime, run = researched
    new, _ = await _restart(runtime, run, "replay")
    found = await views.comparison(
        runtime.runtime, runtime.runtime.tenant, "ai-researcher", run, new
    )
    assert found.same_output
    assert all(step.same_output and step.same_status for step in found.steps)


async def test_a_replay_that_asks_something_else_diverges(
    open_shop: OpenShop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent changed since the run: replay says so, and calls nothing to find out."""
    shop = await open_shop()
    run = await shop.order()
    reads = len(shop.reads)

    async def changed(ctx: Context, task: dict[str, Any]) -> Any:
        if task["do"] == "check":
            return await ctx.tools.invoke("shop.review.read", item=task["item"])
        return await _clerk(ctx, task)

    monkeypatch.setitem(CLERK, "now", changed)
    new = await shop.core.restart("shop", run, "replay")
    result = await RunHandle(shop.runtime, "shop", new).wait()
    assert result.status == "failed"
    assert result.error is not None
    assert result.error["code"] == "replay.diverged"
    assert len(shop.reads) == reads
    failed = [e for e in result.events if e.type == "tool.call.failed"]
    assert failed


async def test_a_replay_with_other_arguments_diverges(
    open_shop: OpenShop, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop = await open_shop()
    run = await shop.order()

    async def changed(ctx: Context, task: dict[str, Any]) -> Any:
        if task["do"] == "check":
            return await ctx.tools.invoke("shop.stock.read", item="pear")
        return await _clerk(ctx, task)

    monkeypatch.setitem(CLERK, "now", changed)
    result = await shop.restart(run, "replay")
    assert result.error is not None
    assert result.error["code"] == "replay.diverged"


async def test_an_agent_sees_a_tools_output_as_the_record_holds_it(
    open_shop: OpenShop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keys sorted, live and in a replay. PostgreSQL's jsonb doesn't keep their order, so an agent
    that put a tool's output in a prompt would otherwise ask something else in a replay."""

    async def labelling(ctx: Context, task: dict[str, Any]) -> Any:
        if task["do"] == "check":
            label = await ctx.tools.invoke("shop.label.read", item=task["item"])
            assert isinstance(label, dict)
            return {"keys": list(label)}
        return await _clerk(ctx, task)

    monkeypatch.setitem(CLERK, "now", labelling)
    shop = await open_shop()
    run = await shop.order()
    assert (await shop.core.run_state("shop", run)).steps["check"].output == {
        "keys": ["alpha", "zeta"]
    }
    new = await shop.core.restart("shop", run, "replay")
    assert (await RunHandle(shop.runtime, "shop", new).wait(timeout=10)).status == "completed"
    assert (await shop.core.run_state("shop", new)).steps["check"].output == {
        "keys": ["alpha", "zeta"]
    }


async def test_a_replay_keeps_its_sources_input(open_shop: OpenShop) -> None:
    shop = await open_shop()
    run = await shop.order()
    result = await shop.restart(run, "replay", input=ORDER | {"item": "pear"})
    assert result.status == "completed"
    started = next(e for e in result.events if e.type == "run.started")
    assert isinstance(started.data, dict)
    assert started.data["input"] == ORDER


async def test_a_replay_meets_the_failures_its_source_met(open_shop: OpenShop) -> None:
    shop = await open_shop()
    shop.till_down = True
    run = await shop.order()
    original = await RunHandle(shop.runtime, "shop", run).wait()
    assert original.status == "failed"
    placed = len(shop.placed)
    result = await shop.restart(run, "replay")
    assert result.status == "failed"
    assert result.error is not None
    assert original.error is not None
    assert result.error["code"] == original.error["code"]
    assert len(shop.placed) == placed


async def test_a_replay_holds_back_side_effects(open_shop: OpenShop) -> None:
    shop = await open_shop()
    run = await shop.order()
    placed, reads = len(shop.placed), len(shop.reads)
    result = await shop.restart(run, "replay")
    assert result.status == "completed"
    assert (len(shop.placed), len(shop.reads)) == (placed, reads)


async def test_a_replay_asks_nobody_again(open_shop: OpenShop) -> None:
    """The signal the source waited for came from outside: the replay keeps its answer."""
    shop = await open_shop()
    run = await shop.order()
    result = await shop.restart(run, "replay")
    assert result.status == "completed"
    assert not [e for e in result.events if e.type == "run.paused"]


async def test_only_a_finished_run_can_be_replayed(open_shop: OpenShop) -> None:
    shop = await open_shop()
    handle = await shop.start()
    await handle.wait_for_event("run.paused")
    with pytest.raises(ConflictError, match="only a finished run"):
        await shop.core.restart("shop", handle.run_id, "replay")


async def test_a_run_that_isnt_there(open_shop: OpenShop) -> None:
    shop = await open_shop()
    with pytest.raises(NotFoundError):
        await shop.core.restart("shop", "01J0000000000000000000000A", "replay")


class Counting(Provider):
    name = "fake"

    def __init__(self, *, down: bool = False) -> None:
        self.calls = 0
        self.down = down

    async def complete(self, entry: ModelEntry, request: ModelRequest) -> Completion:
        self.calls += 1
        if self.down:
            raise ProviderError("the model is down", retryable=False)
        return Completion(text="a name", input_tokens=10, output_tokens=2, model=entry.model)


async def _name(ctx: Context, task: Any) -> str:
    return await ctx.model.generate(messages=f"Name the run {ctx.run_id}.")


NAMER_CODE: dict[str, Callable[[Context, Any], Awaitable[str]]] = {"now": _name}


async def _naming(ctx: Context, task: Any) -> str:
    return await NAMER_CODE["now"](ctx, task)


async def _namer(monkeypatch: pytest.MonkeyPatch, provider: Provider) -> LocalRuntime:
    module = ModuleType("replay_namer")
    module.work = _naming  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "replay_namer", module)
    runtime = LocalRuntime(providers={"fake": provider})
    await runtime.__aenter__()
    runtime.runtime.models.registry.add(ModelEntry(id="fake/one", model="one"))
    await runtime.runtime.apply(load_manifests(NAMER))
    return runtime


async def _name_twice(runtime: LocalRuntime) -> tuple[RunResult, RunResult]:
    run = await runtime.runtime.chat("namer", input="go")
    first = await RunHandle(runtime, "namer", run).wait(timeout=10)
    new = await runtime.runtime.restart("namer", run, "replay")
    return first, await RunHandle(runtime, "namer", new).wait(timeout=10)


async def test_a_runs_own_id_may_differ_from_the_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """A replay is a new run. An agent that puts its run id in a prompt, a path or a key
    (``ctx.step_key``) still replays: that id is the one thing allowed to differ."""
    provider = Counting()
    runtime = await _namer(monkeypatch, provider)
    try:
        first, again = await _name_twice(runtime)
    finally:
        await runtime.__aexit__(None, None, None)
    assert first.status == "completed"
    assert again.status == "completed", again.error
    assert again.output == "a name"
    assert provider.calls == 1


async def test_a_replay_still_sees_a_changed_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    async def changed(ctx: Context, task: Any) -> str:
        return await ctx.model.generate(messages="Name it something else.")

    provider = Counting()
    runtime = await _namer(monkeypatch, provider)
    try:
        run = await runtime.runtime.chat("namer", input="go")
        await RunHandle(runtime, "namer", run).wait(timeout=10)
        monkeypatch.setitem(NAMER_CODE, "now", changed)
        new = await runtime.runtime.restart("namer", run, "replay")
        result = await RunHandle(runtime, "namer", new).wait(timeout=10)
    finally:
        await runtime.__aexit__(None, None, None)
    assert result.error is not None
    assert result.error["code"] == "replay.diverged"
    assert provider.calls == 1
    assert not [e for e in result.events if e.type == "step.retrying"]  # it isn't tried again


async def test_a_replay_meets_the_model_failures_its_source_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = Counting(down=True)
    runtime = await _namer(monkeypatch, provider)
    try:
        first, again = await _name_twice(runtime)
    finally:
        await runtime.__aexit__(None, None, None)
    calls = provider.calls
    assert first.error is not None
    assert again.error is not None
    assert again.error["code"] == first.error["code"] != "replay.diverged"
    assert provider.calls == calls


async def test_a_replay_makes_no_embeddings() -> None:
    """Memory falls back to keywords, as it does with no embedding model: nothing is called."""
    async with LocalRuntime() as runtime:
        asked = task_request("shop.check", tenantid=runtime.runtime.tenant, appid="shop")
        data = asked.data if isinstance(asked.data, dict) else {}
        source = {"run": new_ulid(), "mode": "replay"}
        event = asked.model_copy(update={"data": {**data, "source": source}})
        site = CallSite(event=event, agent="clerk", principal="agent:clerk@acme", index=0)
        with pytest.raises(ModelError, match="no embeddings"):
            await runtime.runtime.models.embed(EmbeddingRequest(texts=["apples"]), site)


# --------------------------------------------------------------------------- fork


async def test_a_fork_keeps_what_comes_before_its_step(open_shop: OpenShop) -> None:
    shop = await open_shop()
    run = await shop.order()
    reads = len(shop.reads)
    result = await shop.restart(run, "fork", step="pay")
    assert result.status == "completed"
    assert len(shop.reads) == reads  # check wasn't run again
    copied = [
        e.data for e in result.events if e.type == "step.completed" and isinstance(e.data, dict)
    ]
    kept = {str(c["step"]) for c in copied if c.get("copied_from") == run}
    assert kept == {"check", "go"}
    order = [c["step"] for c in copied]
    assert order == ["check", "go", "pay", "tell"]
    new = next(e.runid for e in result.events if e.runid)
    drawn = await views.trace(shop.core, shop.tenant, "shop", new or "")
    assert drawn.source is not None
    assert drawn.source["copied"] == ["check", "go"]


async def test_a_fork_holds_back_side_effects(open_shop: OpenShop) -> None:
    shop = await open_shop()
    run = await shop.order()
    placed = len(shop.placed)
    result = await shop.restart(run, "fork", step="pay")
    assert len(shop.placed) == placed
    [call] = [e.data for e in result.events if e.type == "tool.call.completed"]
    assert isinstance(call, dict)
    assert call["stubbed"]
    assert call["output"] == {"receipt": "R1", "amount": 50}  # the same call, so the same answer


async def test_a_stub_asked_something_new_says_it_is_one(open_shop: OpenShop) -> None:
    shop = await open_shop()
    run = await shop.order()
    result = await shop.restart(run, "fork", step="pay", step_input={"do": "pay", "amount": 999})
    [call] = [e.data for e in result.events if e.type == "tool.call.completed"]
    assert isinstance(call, dict)
    assert call["output"] == {"stubbed": True, "tool": "shop.order.place"}
    assert shop.placed == [50]


async def test_a_fork_publishes_no_fact(open_shop: OpenShop) -> None:
    """A fact makes other things happen: Apps, subscribers. A fork's must not."""
    shop = await open_shop()
    run = await shop.order()
    new = await shop.core.restart("shop", run, "fork", step="tell")
    await RunHandle(shop.runtime, "shop", new).wait()
    assert not await shop.events(new, "shop.order.confirmed")
    [held] = await shop.events(new, "agent.emit.stubbed")
    assert isinstance(held.data, dict)
    assert held.data["type"] == "shop.order.confirmed"


async def test_a_fork_stores_no_memory(open_shop: OpenShop) -> None:
    shop = await open_shop()
    run = await shop.order()
    before = len(await shop.core.memory.of_app(shop.tenant, "shop"))
    new = await shop.core.restart("shop", run, "fork", step="tell")
    await RunHandle(shop.runtime, "shop", new).wait()
    assert len(await shop.core.memory.of_app(shop.tenant, "shop")) == before
    [written] = await shop.events(new, "memory.item.written")
    assert isinstance(written.data, dict)
    assert written.data["stubbed"]


async def test_a_fork_keeps_the_taint_of_what_it_copies(open_shop: OpenShop) -> None:
    """Otherwise a fork from after an untrusted read would launder it."""
    shop = await open_shop()
    run = await shop.order(reader="shop.review.read")
    new = await shop.core.restart("shop", run, "fork", step="tell")
    await RunHandle(shop.runtime, "shop", new).wait()
    state = await shop.core.run_state("shop", new)
    assert state.tainted
    assert "tool:shop.review.read" in state.taint


async def test_a_fork_keeps_the_answer_from_outside(open_shop: OpenShop) -> None:
    shop = await open_shop()
    run = await shop.order()
    result = await shop.restart(run, "fork", step="check")
    assert result.status == "completed"
    assert not [e for e in result.events if e.type == "run.paused"]
    done = [
        e.data["step"]
        for e in result.events
        if e.type == "step.completed" and isinstance(e.data, dict)
    ]
    assert done == ["check", "go", "pay", "tell"]  # given when the run got there, not before


async def test_policy_still_decides_in_a_fork(open_shop: OpenShop) -> None:
    shop = await open_shop()
    run = await shop.order()
    await shop.core.apply(load_manifests(SHOP + DENY))
    result = await shop.restart(run, "fork", step="pay")
    assert result.status == "failed"
    assert [e for e in result.events if e.type == "policy.decision.denied"]


async def test_a_fork_asks_no_person_for_a_stub(open_shop: OpenShop) -> None:
    """Nothing would happen, so there is nothing to agree to; the decision is still recorded."""
    shop = await open_shop(ASK)
    run = await shop.order()
    result = await shop.restart(run, "fork", step="pay", step_input={"do": "pay", "amount": 500})
    assert result.status == "completed"
    assert [e for e in result.events if e.type == "policy.decision.escalated"]
    assert not [e for e in result.events if e.type == "approval.requested"]


async def test_a_fork_can_change_the_runs_input(open_shop: OpenShop) -> None:
    shop = await open_shop()
    run = await shop.order()
    new = await shop.core.restart("shop", run, "fork", step="check", input=ORDER | {"item": "pear"})
    await RunHandle(shop.runtime, "shop", new).wait()
    state = await shop.core.run_state("shop", new)
    assert state.steps["check"].output == {"item": "pear", "stock": 7}
    assert shop.reads[-1] == "pear"  # reading changes nothing, so a fork reads for real


async def test_a_fork_keeps_only_what_finished(open_shop: OpenShop) -> None:
    """A step that failed in the source isn't kept: it runs again, held back."""
    shop = await open_shop()
    shop.till_down = True
    run = await shop.order()
    result = await shop.restart(run, "fork", step="tell")
    assert result.status == "completed"
    done = [e.data for e in result.events if e.type == "step.completed"]
    kept = {str(d["step"]) for d in done if isinstance(d, dict) and d.get("copied_from")}
    assert kept == {"check", "go"}
    stubbed = [e.data for e in result.events if e.type == "tool.call.completed"]
    assert [d["tool"] for d in stubbed if isinstance(d, dict) and d["stubbed"]] == [
        "shop.order.place"
    ]
    assert shop.placed == []


async def test_a_stub_is_as_untrusted_as_its_tool(
    open_shop: OpenShop, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop = await open_shop()
    run = await shop.order()

    async def mailing(ctx: Context, task: dict[str, Any]) -> Any:
        if task["do"] == "pay":
            return await ctx.tools.invoke("shop.mail.send", to="ann")
        return await _clerk(ctx, task)

    monkeypatch.setitem(CLERK, "now", mailing)
    assert not (await shop.core.run_state("shop", run)).tainted
    new = await shop.core.restart("shop", run, "fork", step="pay")
    await RunHandle(shop.runtime, "shop", new).wait(timeout=10)
    assert (await shop.core.run_state("shop", new)).tainted


async def test_a_fork_changes_and_erases_no_memory(
    open_shop: OpenShop, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop = await open_shop()
    run = await shop.order()
    [item] = await shop.core.memory.of_app(shop.tenant, "shop")

    async def tidying(ctx: Context, task: dict[str, Any]) -> Any:
        if task["do"] == "tell":
            await ctx.memory.update(task["item"], content="no order was placed")
            await ctx.memory.forget(item=task["item"])
            return "tidied"
        return await _clerk(ctx, task)

    monkeypatch.setitem(CLERK, "now", tidying)
    result = await shop.restart(
        run, "fork", step="tell", step_input={"do": "tell", "item": item.id}
    )
    assert result.status == "completed", result.error
    assert [i.content for i in await shop.core.memory.of_app(shop.tenant, "shop")] == [
        "an order was placed"
    ]


async def test_a_fork_needs_a_step_that_exists(open_shop: OpenShop) -> None:
    shop = await open_shop()
    run = await shop.order()
    with pytest.raises(NotFoundError, match="no step"):
        await shop.core.restart("shop", run, "fork", step="nope")


async def test_a_fork_can_name_the_model(researched: tuple[LocalRuntime, str]) -> None:
    runtime, run = researched
    _, result = await _restart(
        runtime, run, "fork", step="review", model="anthropic/claude-opus-5-5"
    )
    [call] = [e.data for e in result.events if e.type == "model.call.completed"]
    assert isinstance(call, dict)
    assert call["model"] == "claude-opus-5-5"


async def test_a_model_that_isnt_there(researched: tuple[LocalRuntime, str]) -> None:
    runtime, run = researched
    with pytest.raises(NotFoundError, match="no model"):
        await runtime.runtime.restart("ai-researcher", run, "fork", step="review", model="x/y")


# --------------------------------------------------------------------------- retry


async def test_a_retry_does_it_for_real(open_shop: OpenShop) -> None:
    shop = await open_shop()
    shop.till_down = True
    run = await shop.order()
    shop.till_down = False  # mended
    reads = len(shop.reads)
    new = await shop.core.restart("shop", run, "retry")  # from the step that failed
    handle = RunHandle(shop.runtime, "shop", new)
    result = await handle.wait()
    assert result.status == "completed"
    assert shop.placed == [50]
    assert len(shop.reads) == reads
    started = next(e for e in result.events if e.type == "run.started")
    assert isinstance(started.data, dict)
    source = started.data["source"]
    assert isinstance(source, dict)
    assert source["live"]
    assert await shop.events(new, "shop.order.confirmed")  # a retry's facts are real


async def test_a_retry_asks_again(open_shop: OpenShop) -> None:
    shop = await open_shop()
    run = await shop.order()
    new = await shop.core.restart("shop", run, "retry", step="go")
    handle = RunHandle(shop.runtime, "shop", new)
    await handle.wait_for_event("run.paused", timeout=10)
    await shop.core.signal("shop", new, "go")
    assert (await handle.wait()).status == "completed"


async def test_a_retry_needs_something_to_retry(open_shop: OpenShop) -> None:
    shop = await open_shop()
    run = await shop.order()
    with pytest.raises(ConflictError, match="nothing failed"):
        await shop.core.restart("shop", run, "retry")


# --------------------------------------------------------------------------- compare


async def test_two_runs_side_by_side(open_shop: OpenShop) -> None:
    shop = await open_shop()
    run = await shop.order()
    new = await shop.core.restart(
        "shop", run, "fork", step="pay", step_input={"do": "pay", "amount": 999}
    )
    await RunHandle(shop.runtime, "shop", new).wait()
    found = await views.comparison(shop.core, shop.tenant, "shop", run, new)
    steps = {step.step: step for step in found.steps}
    assert steps["check"].same_output
    assert steps["check"].b.copied
    assert not steps["pay"].same_output
    assert steps["pay"].b.stubbed == 1
    assert found.status == ("completed", "completed")
