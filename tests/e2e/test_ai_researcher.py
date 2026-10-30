"""Acceptance tests for the AI Researcher reference app (apps/ai-researcher, stories R1-R7).

They are written against the in-process test runtime planned for WP 1.7
(``aios.testing.runtime.LocalRuntime``: in-memory bus, kernel, engine, recorded model
cassettes). Each test is expected to fail with ImportError until that runtime exists; once its
work packages land, remove the module-level xfail and the test must pass.
"""
# pyright: basic, reportMissingImports=false

import pytest

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.xfail(
        raises=ImportError,
        reason="WP 1.2-1.8: runtime, SDK and reference app not implemented yet",
    ),
]

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


async def test_survives_a_worker_crash_without_repeating_work() -> None:
    from aios.testing.runtime import LocalRuntime

    async with LocalRuntime.from_app(APP, cassettes=CASSETTES, durable=True) as runtime:
        run = await runtime.chat("ai-researcher", QUESTION)
        await run.wait_for_event("task.completed", capability="research.web")
        await runtime.kill_worker()
        await runtime.restart_worker()
        result = await run.wait(timeout=180)

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

    async with (
        fake_mcp_server(tools=["search.web.query"]) as server,
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
