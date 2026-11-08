"""OpenAI Agents SDK agents as AIOS agents (FR-INT-04; WP 3.6)."""

import json
from typing import Any

import pytest
from agents import Agent, WebSearchTool
from pydantic import BaseModel

from aios.adapters.openai_agents import (  # pyright: ignore[reportPrivateUsage]
    _input,
    _turns,
)
from aios.core.ports import ModelRequest
from aios.models import Completion, ModelEntry

from .conftest import Scripted


def build(tools: list[Any]) -> Agent[Any]:
    return Agent(name="Helper", instructions="Answer from the help articles.", tools=tools)


async def test_an_agent_answers_through_aios(desk: Any, module: Any) -> None:
    where = module("oa_helper", {"build": build})
    found = await desk("openai-agents", f"{where}:build")
    result = await (await found.runtime.chat("helpdesk", "How long do refunds take?")).wait(
        timeout=15
    )
    assert result.status == "completed", result.error
    assert result.output == {"answer": "Refunds take 30 days."}
    assert found.asked == ["refunds"]  # through Tool OS, once
    first, second = found.model.requests
    assert first.system == "Answer from the help articles."
    assert [t.name for t in first.tools] == ["kb__article__find"]
    assert first.task == "planning"
    assert [m.role for m in second.messages] == ["user", "assistant", "tool"]
    assert json.loads(second.messages[2].content) == {"days": 30}
    types = [e.type for e in result.events]
    assert types.count("model.call.completed") == 2
    assert types.count("tool.call.completed") == 1


class Verdict(BaseModel):
    refundable: bool
    days: int


class Structured(Scripted):
    async def complete(self, entry: ModelEntry, request: ModelRequest) -> Completion:
        self.requests.append(request)
        return Completion(
            text='{"refundable": true, "days": 30}',
            input_tokens=5,
            output_tokens=5,
            model=entry.model,
        )


async def test_structured_output_is_the_output(desk: Any, module: Any) -> None:
    agent = Agent(name="Judge", instructions="Decide.", output_type=Verdict)
    where = module("oa_judge", {"agent": agent})
    found = await desk("openai-agents", f"{where}:agent", Structured())
    result = await (await found.runtime.chat("helpdesk", "Order 1001?")).wait(timeout=15)
    assert result.status == "completed", result.error
    assert result.output == {"refundable": True, "days": 30}
    [asked] = found.model.requests
    assert asked.output_schema is not None
    assert set(asked.output_schema["properties"]) == {"refundable", "days"}


async def test_a_tool_hosted_by_openai_is_refused(desk: Any, module: Any) -> None:
    agent = Agent(name="Searcher", instructions="Search.", tools=[WebSearchTool()])
    where = module("oa_searcher", {"agent": agent})
    found = await desk("openai-agents", f"{where}:agent")
    result = await (await found.runtime.chat("helpdesk", "Search the web")).wait(timeout=15)
    assert result.status == "failed"
    assert "runs outside AIOS" in str(result.error)
    assert found.model.requests == []  # nothing was asked of any model


def test_input_items_become_turns() -> None:
    system, turns = _turns(
        [
            {"role": "developer", "content": "Be brief."},
            {"role": "user", "content": "Refund window?"},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Checking."}],
            },
            {"type": "function_call", "call_id": "c1", "name": "kb__find", "arguments": '{"q": 1}'},
            {"type": "function_call", "call_id": "c2", "name": "kb__find", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "a"},
            {"type": "function_call_output", "call_id": "c2", "output": "b"},
            {"type": "reasoning", "summary": []},
        ]
    )
    assert system == "Be brief."
    assert [t.role for t in turns] == ["user", "assistant", "tool", "tool"]
    assert turns[1].content == "Checking."
    assert [c.id for c in turns[1].tool_calls] == ["c1", "c2"]  # one assistant turn


@pytest.mark.parametrize(
    ("task", "said"),
    [("text", "text"), ({"message": "hi"}, "hi"), ({"order": 1}, '{"order": 1}')],
)
def test_a_task_is_the_users_turn(task: Any, said: str) -> None:
    assert _input(task) == said


async def test_nothing_goes_to_openais_tracing(
    desk: Any, module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agents import RunConfig, Runner  # noqa: PLC0415

    seen: list[RunConfig] = []
    real = Runner.run

    async def watched(*args: Any, run_config: RunConfig, **kwargs: Any) -> Any:
        seen.append(run_config)
        return await real(*args, run_config=run_config, **kwargs)

    monkeypatch.setattr(Runner, "run", watched)
    where = module("oa_traced", {"build": build})
    found = await desk("openai-agents", f"{where}:build")
    result = await (await found.runtime.chat("helpdesk", "Hello")).wait(timeout=15)
    assert result.status == "completed", result.error
    [config] = seen
    assert config.tracing_disabled is True


async def test_a_handoff_is_offered_as_a_tool(desk: Any, module: Any) -> None:
    billing = Agent(name="Billing", instructions="Handle billing.")
    triage = Agent(name="Triage", instructions="Route the question.", handoffs=[billing])
    where = module("oa_triage", {"agent": triage})
    found = await desk("openai-agents", f"{where}:agent")
    await (await found.runtime.chat("helpdesk", "My invoice is wrong")).wait(timeout=15)
    first = found.model.requests[0]
    assert "transfer_to_billing" in [t.name for t in first.tools]
