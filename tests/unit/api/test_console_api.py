"""What the console reads (WP 3.2): a run's trace, costs, agents and policies, on a real run."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from aios.api import create_app
from aios.controlplane import Runtime, load_app, views
from aios.core.catalog import ModelCallCompleted, Usage
from aios.core.envelope import Event
from aios.core.identity import Identity, Suspendable
from aios.core.manifests import AppBudgets
from aios.fabric import InMemoryEventBus, Scope, StoredEvent
from aios.testing.runtime import LocalRuntime

ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / "apps" / "ai-researcher"
CASSETTES = ROOT / "tests" / "cassettes" / "ai-researcher"
NAME = "ai-researcher"
QUESTION = "What changed in the OpenTelemetry GenAI semantic conventions in 2026?"
STEPS = ["plan", "research", "analyze", "draft", "review"]


@dataclass
class Researched:
    http: httpx2.AsyncClient
    runtime: LocalRuntime
    run: str

    async def get(self, path: str, **params: Any) -> Any:
        answer = await self.http.get(path, params=params)
        assert answer.status_code == 200, answer.text
        return answer.json()

    @property
    def tenant(self) -> str:
        return self.runtime.runtime.tenant


@pytest.fixture
async def done() -> AsyncIterator[Researched]:
    async with LocalRuntime.from_app(APP, cassettes=CASSETTES) as runtime:
        transport = httpx2.ASGITransport(app=create_app(runtime.runtime))
        async with httpx2.AsyncClient(transport=transport, base_url="http://aios") as http:
            started = await http.post(f"/v1/apps/{NAME}/runs", json={"message": QUESTION})
            run = started.json()["run_id"]
            async with asyncio.timeout(30):
                while (await http.get(f"/v1/apps/{NAME}/runs/{run}")).json()["status"] == "running":  # noqa: ASYNC110 - the API is all there is to ask
                    await asyncio.sleep(0.05)
            yield Researched(http, runtime, run)


# --------------------------------------------------------------------------- the trace


async def test_a_trace_draws_the_workflow_it_ran(done: Researched) -> None:
    trace = await done.get(f"/v1/apps/{NAME}/runs/{done.run}/trace")
    assert (trace["workflow"], trace["version"], trace["status"]) == (
        "research-report",
        "1.1.0",
        "completed",
    )
    assert [node["id"] for node in trace["nodes"]] == STEPS
    edges = {(e["source"], e["target"], e["loop"]) for e in trace["edges"]}
    assert ("plan", "research", False) in edges
    assert ("review", "draft", True) in edges  # the bounded loop back
    assert all(node["status"] == "completed" for node in trace["nodes"])


async def test_each_step_shows_what_it_cost(done: Researched) -> None:
    trace = await done.get(f"/v1/apps/{NAME}/runs/{done.run}/trace")
    run = await done.get(f"/v1/apps/{NAME}/runs/{done.run}")
    plan = trace["nodes"][0]
    [call, *_] = plan["model_calls"]
    assert call["input_tokens"] > 0
    assert call["provider"]
    assert plan["input_tokens"] == sum(c["input_tokens"] for c in plan["model_calls"])
    assert trace["cost_usd"] == pytest.approx(sum(n["cost_usd"] for n in trace["nodes"]))
    assert trace["cost_usd"] == pytest.approx(run["usage"]["cost_usd"])
    assert all(node["duration_ms"] >= 0 for node in trace["nodes"])


async def test_tool_calls_and_their_decisions_sit_with_their_step(done: Researched) -> None:
    trace = await done.get(f"/v1/apps/{NAME}/runs/{done.run}/trace")
    research = next(n for n in trace["nodes"] if n["id"] == "research")
    assert research["tool_calls"]
    assert {call["status"] for call in research["tool_calls"]} <= {"completed", "failed"}
    assert {d["action"] for d in research["decisions"]} == {"tool.invoke"}
    assert all(not n["tool_calls"] for n in trace["nodes"] if n["id"] != "research")


async def test_the_timeline_has_every_event_in_order(done: Researched) -> None:
    trace = await done.get(f"/v1/apps/{NAME}/runs/{done.run}/trace")
    events = await done.get(f"/v1/apps/{NAME}/runs/{done.run}/events")
    timeline = trace["timeline"]
    assert [m["id"] for m in timeline] == [e["id"] for e in events]
    assert timeline[0]["offset_ms"] == 0
    offsets = [m["offset_ms"] for m in timeline]
    assert offsets == sorted(offsets)
    assert offsets[-1] > 0  # the run took time, and the timeline says how much
    assert {m["step"] for m in timeline} >= {None, *STEPS}


async def test_a_removed_apps_runs_have_no_trace(done: Researched) -> None:
    """As with every other view of a run: the App has to be there."""
    await done.runtime.runtime.remove(done.tenant, NAME)
    gone = await done.http.get(f"/v1/apps/{NAME}/runs/{done.run}/trace")
    assert gone.status_code == 404


async def test_a_trace_of_nothing_is_not_found(done: Researched) -> None:
    missing = await done.http.get(f"/v1/apps/{NAME}/runs/01J0000000000000000000000A/trace")
    assert missing.status_code == 404
    other = await done.http.get(f"/v1/apps/nope/runs/{done.run}/trace")
    assert other.status_code == 404


# --------------------------------------------------------------------------- costs


async def test_costs_add_up_to_the_runs(done: Researched) -> None:
    costs = await done.get("/v1/costs")
    trace = await done.get(f"/v1/apps/{NAME}/runs/{done.run}/trace")
    assert costs["cost_usd"] == pytest.approx(trace["cost_usd"])
    [app] = costs["by_app"]
    assert app["key"] == NAME
    assert app["calls"] == sum(len(n["model_calls"]) for n in trace["nodes"])
    agents = {spend["key"] for spend in costs["by_agent"]}
    assert "planner-agent" in agents
    assert sum(s["cost_usd"] for s in costs["by_model"]) == pytest.approx(costs["cost_usd"])


async def test_another_month_costs_nothing(done: Researched) -> None:
    costs = await done.get("/v1/costs", month="2020-01")
    assert (costs["cost_usd"], costs["by_app"], costs["budgets"]) == (0, [], [])


@pytest.mark.parametrize("month", ["2026-13", "2026-1", "26-11", "nonsense"])
async def test_a_month_must_look_like_one(done: Researched, month: str) -> None:
    assert (await done.http.get("/v1/costs", params={"month": month})).status_code == 422


async def test_budgets_show_what_is_spent_and_where_soft_begins(done: Researched) -> None:
    budgets = AppBudgets.model_validate({"monthlyUSD": 5})
    done.runtime.runtime.models.use_budgets(done.tenant, NAME, budgets)
    costs = await done.get("/v1/costs")
    [app] = [b for b in costs["budgets"] if b["level"] == "app"]
    assert (app["limit_usd"], app["soft_usd"]) == (5, 4)
    assert app["spent_usd"] == pytest.approx(costs["cost_usd"])


# --------------------------------------------------------------------------- agents


async def test_agents_are_listed_with_their_work(done: Researched) -> None:
    agents = {a["agent"]: a for a in await done.get("/v1/agents")}
    assert set(agents) == {
        "planner-agent",
        "research-agent",
        "analyst-agent",
        "writer-agent",
        "reviewer-agent",
    }
    research = agents["research-agent"]
    assert (research["version"], research["capabilities"]) == ("0.1.0", ["research.web"])
    assert research["tools"] == ["search.web.query", "http.page.fetch"]
    assert research["completed"] > 0
    assert research["last_active"] is not None
    assert not research["suspended"]


async def test_only_the_callers_tenant_agents_are_listed(tmp_path: Path) -> None:
    async with LocalRuntime.from_app(APP, tenants=["acme", "globex"]) as runtime:
        mine = await views.agents_of(runtime.runtime, "acme")
        assert {agent.app for agent in mine} == {NAME}
        assert len(mine) == 5  # not ten: globex has the same App, and its agents aren't acme's


def _call(tenant: str, when: datetime) -> Event:
    usage = Usage(cost_usd=0.01, input_tokens=10, output_tokens=5)
    payload = ModelCallCompleted(
        call=0, site="s", agent="planner-agent", provider="p", model="m", usage=usage,
        latency_ms=1.0, output_text="",
    )  # fmt: skip
    return Event(
        source=f"aios://{tenant}/apps/{NAME}/models",
        type="model.call.completed",
        tenantid=tenant,
        appid=NAME,
        principal=f"agent:planner-agent@{tenant}",
        time=when,
        data=payload.model_dump(mode="json"),
    )


async def test_the_cost_report_stops_reading_at_the_months_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading back to the start of time would cost more each month the log grows."""
    async with LocalRuntime.from_app(APP) as runtime:
        tenant, now = runtime.runtime.tenant, datetime.now(UTC)
        await runtime.bus.publish(
            *(_call(tenant, datetime(2020, 1, 1, tzinfo=UTC)) for _ in range(20))
        )
        await runtime.bus.publish(*(_call(tenant, now) for _ in range(3)))
        real = runtime.bus.latest
        read: list[int] = []

        async def counting(scope: Scope, pattern: str = ">") -> AsyncIterator[StoredEvent]:
            async for stored in real(scope, pattern):
                read.append(1)
                yield stored

        monkeypatch.setattr(runtime.bus, "latest", counting)
        report = await views.costs(runtime.runtime, tenant, f"{now:%Y-%m}")
    assert report.calls == 3
    assert len(read) == 4  # this month's three, and the first one before it


async def test_a_call_stamped_just_before_the_month_by_a_slow_clock_isnt_lost() -> None:
    async with LocalRuntime.from_app(APP) as runtime:
        tenant, now = runtime.runtime.tenant, datetime.now(UTC)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        await runtime.bus.publish(_call(tenant, now))
        await runtime.bus.publish(_call(tenant, start - timedelta(minutes=5)))  # a lagging clock
        await runtime.bus.publish(_call(tenant, now))
        report = await views.costs(runtime.runtime, tenant, f"{now:%Y-%m}")
    assert report.calls == 2  # both of this month's, though an older one was stored between


async def test_a_suspended_agent_says_why(done: Researched) -> None:
    admin = Identity("user:ada", done.tenant, frozenset({"tenant-admin"}))
    await done.runtime.runtime.suspend(admin, Suspendable.AGENT, "writer-agent", reason="drafts")
    agents = {a["agent"]: a for a in await done.get("/v1/agents")}
    assert agents["writer-agent"]["suspended"]
    assert "drafts" in agents["writer-agent"]["suspension"]
    assert not agents["planner-agent"]["suspended"]


async def test_a_suspended_app_stops_all_its_agents(done: Researched) -> None:
    admin = Identity("user:ada", done.tenant, frozenset({"tenant-admin"}))
    await done.runtime.runtime.suspend(admin, Suspendable.APP, NAME, reason="audit")
    assert all(a["suspended"] for a in await done.get("/v1/agents"))


# --------------------------------------------------------------------------- policies


async def test_an_apps_policies_are_shown(done: Researched) -> None:
    view = await done.get(f"/v1/apps/{NAME}/policies")
    names = {policy["metadata"]["name"] for policy in view["policies"]}
    assert "research-agent" in names  # its allow list, as a policy
    assert "permit" in view["cedar"]
    assert view["rules"]
    assert all(policy["kind"] == "Policy" for policy in view["policies"])


async def _ask(done: Researched, **question: Any) -> Any:
    answer = await done.http.post(f"/v1/apps/{NAME}/policies/simulate", json=question)
    assert answer.status_code == 200, answer.text
    return answer.json()


async def test_simulating_what_an_agent_may_do(done: Researched) -> None:
    allowed = await _ask(
        done, action="tool.invoke", agent="research-agent", resource="search.web.query"
    )
    assert allowed["effect"] == "allow"
    assert allowed["policies"]


async def test_simulating_what_it_may_not(done: Researched) -> None:
    refused = await _ask(
        done,
        action="tool.invoke",
        agent="writer-agent",
        resource="search.web.query",
    )
    assert refused["effect"] == "deny"


async def test_an_unknown_tool_needs_its_side_effect(done: Researched) -> None:
    question = {"action": "tool.invoke", "agent": "research-agent", "resource": "pay.refund.create"}
    missing = await done.http.post(f"/v1/apps/{NAME}/policies/simulate", json=question)
    assert missing.status_code == 404
    answered = await _ask(done, **question, side_effect="financial")
    assert answered["effect"] == "deny"


async def test_the_kill_switch_counts_in_a_simulation(done: Researched) -> None:
    admin = Identity("user:ada", done.tenant, frozenset({"tenant-admin"}))
    await done.runtime.runtime.suspend(admin, Suspendable.AGENT, "research-agent", reason="x")
    refused = await _ask(
        done, action="tool.invoke", agent="research-agent", resource="search.web.query"
    )
    assert refused["effect"] == "deny"
    assert refused["policies"] == ["suspension/agent/research-agent"]


async def test_the_rules_shown_are_the_apps_own(done: Researched) -> None:
    view = await done.get(f"/v1/apps/{NAME}/policies")
    names = {policy["metadata"]["name"] for policy in view["policies"]}
    assert all(rule["id"].split("/")[0] in names for rule in view["rules"])
    assert not any(rule["id"].startswith("role-") for rule in view["rules"])


async def test_an_api_process_knows_the_built_in_tools(tmp_path: Path) -> None:
    """The API serves no tools, so nothing is bound there; the simulator must still know them."""
    async with InMemoryEventBus() as bus:
        runtime = Runtime(bus, tenant="acme", sandbox=tmp_path)
        runtime.install(load_app(APP))
        question = views.Simulation(
            action="tool.invoke", agent="research-agent", resource="filesystem.file.write"
        )
        verdict = await views.simulate(runtime, "acme", NAME, question)
        assert verdict.effect == "deny"  # known, and not on its list
        spec = await runtime.tool_spec("filesystem.file.write", "acme")
        assert spec is not None
        assert spec.side_effect.value == "write"
        assert await runtime.tool_spec("pay.refund.create", "acme") is None


async def test_the_simulator_weighs_taint_and_approval(tmp_path: Path) -> None:
    """Asked as if the write moved money: after untrusted content, a person must agree."""
    async with LocalRuntime.from_app(ROOT / "tests" / "fixtures" / "apps" / "notes") as runtime:
        tenant = runtime.runtime.tenant

        async def ask(**fields: object) -> str:
            asked = {"action": "tool.invoke", "agent": "notes-agent", "side_effect": "financial"}
            question = views.Simulation.model_validate(
                asked | {"resource": "filesystem.file.write"} | fields
            )
            return (await views.simulate(runtime.runtime, tenant, "notes", question)).effect

        assert await ask() == "allow"
        assert await ask(tainted=True) == "require_approval"
        assert await ask(tainted=True, approved=True) == "allow"


async def test_a_removed_app_leaves_no_policies_behind(done: Researched) -> None:
    await done.runtime.runtime.remove(done.tenant, NAME)
    assert done.runtime.runtime.enforcer.policy_set(done.tenant, NAME) is None
    sources = done.runtime.runtime.enforcer._sources  # pyright: ignore[reportPrivateUsage]
    assert (done.tenant, NAME) not in sources


async def test_a_simulation_leaves_no_trace(done: Researched) -> None:
    before = await done.get("/v1/events", limit=5000)
    await _ask(done, action="tool.invoke", agent="research-agent", resource="search.web.query")
    await _ask(done, action="memory.read", agent="research-agent", resource="app")
    assert await done.get("/v1/events", limit=5000) == before


async def test_simulating_for_an_app_that_isnt_there(done: Researched) -> None:
    question = {"action": "model.route", "agent": "x"}
    assert (
        await done.http.post("/v1/apps/nope/policies/simulate", json=question)
    ).status_code == 404
    assert (await done.http.get("/v1/apps/nope/policies")).status_code == 404


# --------------------------------------------------------------------------- events


async def test_the_newest_events_come_first(done: Researched) -> None:
    newest = await done.get("/v1/events", newest="true", limit=10)
    positions = [event["position"] for event in newest]
    assert len(positions) == 10
    assert positions == sorted(positions, reverse=True)
    oldest = await done.get("/v1/events", limit=10)
    assert oldest[0]["position"] < positions[-1]
