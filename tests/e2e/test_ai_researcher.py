"""Acceptance tests for the AI Researcher reference app (apps/ai-researcher, stories R1-R7).

They run against the in-process test runtime (``aios.testing.runtime.LocalRuntime``: the real
kernel, engine, Model Kernel and Tool OS on the in-memory fabric) with recorded model
cassettes. Written in Phase 0 as expected failures; green since M1.
"""
# pyright: basic, reportIndexIssue=false, reportArgumentType=false, reportCallIssue=false, reportOptionalSubscript=false, reportOptionalMemberAccess=false, reportOperatorIssue=false
# Stories index event payloads (JSON) directly, as a reader of the event log would.

import asyncio

import pytest

pytestmark = [pytest.mark.acceptance]

APP = "apps/ai-researcher"
CASSETTES = "tests/cassettes/ai-researcher"
QUESTION = "What changed in the OpenTelemetry GenAI semantic conventions in 2026?"


async def test_answers_a_question_with_cited_sources() -> None:
    from aios.testing.runtime import LocalRuntime

    async with LocalRuntime.from_app(APP, cassettes=CASSETTES) as runtime:
        run = await runtime.chat("ai-researcher", QUESTION)
        result = await run.wait(timeout=120)

    assert result.status == "completed"
    assert result.output["summary"]
    assert result.output["sources"]
    assert result.events[-1].type == "run.completed"


async def test_event_chain_matches_the_reference_flow() -> None:
    from aios.testing.runtime import LocalRuntime

    async with LocalRuntime.from_app(APP, cassettes=CASSETTES) as runtime:
        result = await (await runtime.chat("ai-researcher", QUESTION)).wait(timeout=120)

    ids = {event.id for event in result.events}
    for event in result.events:
        assert event.type == "app.request.received" or event.causationid in ids
    requested = [e.data["capability"] for e in result.events if e.type == "task.requested"]
    assert requested[0] == "planning"
    assert requested.count("research.web") >= 2
    assert requested[-1] == "review"


def _sites_of(request: dict) -> str:
    """The start of the call-site keys of one task (``CallSite.key``)."""
    item = f"#{request['item']}" if request.get("item") is not None else ""
    return f"{request['step']}{item}/"


async def test_survives_a_worker_crash_without_repeating_work() -> None:
    """M1 exit 2: no completed step runs again, and no model call of a completed step is made
    again. Nor is any call whose answer was stored before the crash (ADR-005).

    A task still running when the worker dies runs again, and a call it had made whose answer
    wasn't stored yet is made again, once: nothing recorded it. That depends on where the kill
    lands, which on a CI runner it did often enough to fail an assertion of no repeats at all.
    """
    from aios.testing.runtime import LocalRuntime

    async with LocalRuntime.from_app(APP, cassettes=CASSETTES, durable=True) as runtime:
        run = await runtime.chat("ai-researcher", QUESTION)
        await run.wait_for_event("task.completed", capability="research.web")
        await runtime.kill_worker()
        at_crash = await run.events()
        await runtime.restart_worker()
        result = await run.wait(timeout=180)

    assert result.status == "completed"
    completed_steps = [e.data["step"] for e in result.events if e.type == "step.completed"]
    assert len(completed_steps) == len(set(completed_steps))

    requests = {e.id: e.data for e in at_crash if e.type == "task.requested"}
    finished = tuple(
        _sites_of(requests[e.causationid])
        for e in at_crash
        if e.type == "task.completed" and e.causationid in requests
    )
    assert finished  # the crash came after a research task had finished
    stored = {
        str(e.data["site"])
        for e in at_crash
        if e.type in ("model.call.completed", "tool.call.completed")
    }
    repeated = result.cassette.repeated_calls
    assert [site for site in repeated if site.startswith(finished)] == []
    assert [site for site in repeated if site in stored] == []
    assert max(result.cassette.plays.values()) <= 2


async def test_a_paused_run_survives_a_worker_crash_and_finishes_when_resumed() -> None:
    from aios.testing.runtime import LocalRuntime

    async with LocalRuntime.from_app(APP, cassettes=CASSETTES, durable=True) as runtime:
        for worker in runtime.runtime.workers:  # a recorded run is over in milliseconds otherwise
            await worker.kill()
        run = await runtime.chat("ai-researcher", QUESTION)
        await run.wait_for_event("task.requested", capability="planning")
        await runtime.runtime.pause("ai-researcher", run.run_id)
        await run.wait_for_event("run.paused")
        await runtime.kill_worker()
        await runtime.restart_worker()
        await run.wait_for_event("task.completed", capability="planning")
        await asyncio.sleep(0.2)
        held = await run.events()
        await runtime.kill_worker()
        await runtime.restart_worker()
        await runtime.runtime.resume("ai-researcher", run.run_id)
        result = await run.wait(timeout=180)

    assert [e.data["capability"] for e in held if e.type == "task.requested"] == ["planning"]
    assert result.status == "completed"
    completed_steps = [e.data["step"] for e in result.events if e.type == "step.completed"]
    assert len(completed_steps) == len(set(completed_steps))
    assert result.cassette.repeated_calls == []


async def test_model_policy_change_reroutes_the_planner() -> None:
    from aios.testing.runtime import LocalRuntime

    async with LocalRuntime.from_app(APP, cassettes=CASSETTES) as runtime:
        before = await (await runtime.chat("ai-researcher", QUESTION)).wait(timeout=120)
        runtime.apply_model_policy_rule(task="planning", candidates=["google/<general-model>"])
        after = await (await runtime.chat("ai-researcher", QUESTION)).wait(timeout=120)

    assert before.model_calls(step="plan")[0].provider != "google"
    assert after.model_calls(step="plan")[0].provider == "google"


async def test_mcp_tools_are_usable_after_one_config_entry() -> None:
    from aios.testing.runtime import LocalRuntime, fake_mcp_server

    pages = [  # what the recorded answers cite: a report can only cite what search returned
        {"title": name, "url": f"https://opentelemetry.io/docs/specs/semconv/gen-ai/{name}"}
        for name in ("", "gen-ai-spans/", "gen-ai-metrics/", "gen-ai-agent-spans/")
    ]
    responses = {"search.web.query": lambda _: pages}
    async with (
        fake_mcp_server(tools=["search.web.query"], responses=responses) as server,
        LocalRuntime.from_app(APP, cassettes=CASSETTES) as runtime,
    ):
        runtime.register_tool_provider("search", mcp_url=server.url)
        result = await (await runtime.chat("ai-researcher", QUESTION)).wait(timeout=120)

    assert result.status == "completed"
    assert server.calls


async def test_mutually_triggering_agents_stop_at_the_hop_limit() -> None:
    from aios.testing.runtime import LocalRuntime, ping_pong_agents

    async with LocalRuntime(agents=ping_pong_agents()) as runtime:
        events = await runtime.publish_and_collect("ping.sent", until="budget.exceeded")

    assert events[-1].type == "budget.exceeded"
    assert max(e.hopcount for e in events) == runtime.bus.max_hops


async def test_run_cost_is_the_sum_of_model_calls() -> None:
    from aios.testing.runtime import LocalRuntime

    async with LocalRuntime.from_app(APP, cassettes=CASSETTES) as runtime:
        result = await (await runtime.chat("ai-researcher", QUESTION)).wait(timeout=120)

    calls = [e for e in result.events if e.type == "model.call.completed"]
    assert calls
    assert all(c.data["usage"]["cost_usd"] >= 0 for c in calls)
    assert result.usage.cost_usd == pytest.approx(sum(c.data["usage"]["cost_usd"] for c in calls))
