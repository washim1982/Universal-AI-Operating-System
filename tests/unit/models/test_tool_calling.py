"""Tool calling through the Model Kernel (FR-MOD-01; WP 3.6).

A model only asks for a tool call. The agent makes it, through Tool OS, where policy applies.
"""

import json
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx2
import pytest

from aios.core.cassettes import Cassette, request_hash
from aios.core.catalog import ModelCallCompleted, ModelToolCall
from aios.core.manifests import load_manifests
from aios.core.policy import SideEffect
from aios.core.ports import Message, ModelRequest, ModelTool, model_tool_name, tool_name_of
from aios.fabric import InMemoryEventBus, Scope
from aios.kernel import Context
from aios.models import (
    AnthropicProvider,
    Completion,
    ModelEntry,
    ModelKernel,
    OpenAIProvider,
    Provider,
    ProviderError,
)
from aios.models.gemini import GeminiProvider
from aios.testing import task_request
from aios.testing.runtime import LocalRuntime
from aios.tools import NativeProvider, tool

ENTRY = ModelEntry(id="any/model", model="model")
SEARCH = ModelTool(
    name="kb__article__find",
    description="Find help articles.",
    parameters={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
)
CALL = ModelToolCall(id="call_1", name="kb__article__find", arguments={"q": "refunds"})
CONVERSATION = [
    Message(role="user", content="How long do refunds take?"),
    Message(role="assistant", content="Let me look.", tool_calls=[CALL]),
    Message(role="tool", tool_call_id="call_1", content='{"days": 30}'),
]


def _request(**fields: Any) -> ModelRequest:
    return ModelRequest(
        messages=CONVERSATION, tools=[SEARCH], tool_choice="required", max_tokens=50, **fields
    )


def _provider(
    make: Callable[[httpx2.AsyncClient], Provider], body: dict[str, Any], seen: list[Any]
):  # type: ignore[no-untyped-def]
    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json=body)

    return make(httpx2.AsyncClient(transport=httpx2.MockTransport(handle)))


# ---------------------------------------------------------------------- each provider's own shape


async def test_claude_is_offered_tools_and_asks_for_one() -> None:
    seen: list[Any] = []
    body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "model",
        "content": [
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "toolu_2", "name": "kb__article__find", "input": {"q": "x"}},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 7, "output_tokens": 2},
    }
    claude = _provider(
        lambda c: AnthropicProvider(api_key="XXXXXX", http_client=c, max_retries=0), body, seen
    )
    done = await claude.complete(ENTRY, _request())
    assert done.text == "Checking."
    assert done.tool_calls == (
        ModelToolCall(id="toolu_2", name="kb__article__find", arguments={"q": "x"}),
    )
    [sent] = seen
    assert sent["tools"] == [
        {
            "name": "kb__article__find",
            "description": "Find help articles.",
            "input_schema": SEARCH.parameters,
        }
    ]
    assert sent["tool_choice"] == {"type": "any"}
    assert sent["messages"][1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Let me look."},
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "kb__article__find",
                "input": {"q": "refunds"},
            },
        ],
    }
    assert sent["messages"][2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": '{"days": 30}'}],
    }


async def test_openai_is_offered_functions_and_asks_for_one() -> None:
    seen: list[Any] = []
    body = {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_9",
                            "type": "function",
                            "function": {"name": "kb__article__find", "arguments": '{"q": "y"}'},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
    }
    gpt = _provider(
        lambda c: OpenAIProvider(api_key="XXXXXX", http_client=c, max_retries=0), body, seen
    )
    done = await gpt.complete(ENTRY, _request())
    assert done.tool_calls == (
        ModelToolCall(id="call_9", name="kb__article__find", arguments={"q": "y"}),
    )
    [sent] = seen
    assert sent["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "kb__article__find",
                "description": "Find help articles.",
                "parameters": SEARCH.parameters,
            },
        }
    ]
    assert sent["tool_choice"] == "required"
    assert sent["messages"][1]["tool_calls"][0]["function"] == {
        "name": "kb__article__find",
        "arguments": '{"q": "refunds"}',
    }
    assert sent["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"days": 30}',
    }


async def test_arguments_that_arent_json_are_worth_another_try() -> None:
    body = {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c",
                            "type": "function",
                            "function": {"name": "kb__article__find", "arguments": "{q: y"},
                        }
                    ],
                },
            }
        ],
    }
    gpt = _provider(
        lambda c: OpenAIProvider(api_key="XXXXXX", http_client=c, max_retries=0), body, []
    )
    with pytest.raises(ProviderError, match="aren't JSON") as raised:
        await gpt.complete(ENTRY, _request())
    assert raised.value.retryable


async def test_gemini_is_offered_functions_and_asks_for_one() -> None:
    seen: list[Any] = []
    body = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "id": "fc_1",
                                "name": "kb__article__find",
                                "args": {"q": "z"},
                            }
                        }
                    ],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2},
    }
    gemini = _provider(lambda c: GeminiProvider(api_key="XXXXXX", http_client=c), body, seen)
    done = await gemini.complete(ENTRY, _request())
    assert done.tool_calls == (
        ModelToolCall(id="fc_1", name="kb__article__find", arguments={"q": "z"}),
    )
    assert done.text == ""
    [sent] = seen
    [declared] = sent["tools"][0]["functionDeclarations"]
    schema = declared.get("parametersJsonSchema", declared.get("parameters_json_schema"))
    assert (declared["name"], schema) == ("kb__article__find", SEARCH.parameters)
    assert sent["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"
    model_turn, answer_turn = sent["contents"][1], sent["contents"][2]
    assert model_turn["role"] == "model"
    assert model_turn["parts"][1]["functionCall"]["name"] == "kb__article__find"
    assert answer_turn["parts"][0]["functionResponse"] == {
        "id": "call_1",
        "name": "kb__article__find",
        "response": {"result": '{"days": 30}'},
    }


# ---------------------------------------------------------------------- what a turn may say


def test_turns_must_make_sense() -> None:
    with pytest.raises(ValueError, match="only an assistant"):
        Message(role="user", content="x", tool_calls=[CALL])
    with pytest.raises(ValueError, match="tool_call_id"):
        Message(role="tool", content="x")
    with pytest.raises(ValueError, match="tool_call_id"):
        Message(role="user", content="x", tool_call_id="call_1")
    with pytest.raises(ValueError, match="share a name"):
        ModelRequest(messages=CONVERSATION, tools=[SEARCH, SEARCH])
    with pytest.raises(ValueError, match="pattern"):
        ModelTool(name="kb.article.find")  # providers allow no dots


def test_a_model_sees_tool_names_it_can_use() -> None:
    assert model_tool_name("search.web.query") == "search__web__query"
    assert tool_name_of("search__web__query") == "search.web.query"


def test_a_request_without_tools_hashes_as_it_always_did() -> None:
    """Recordings made before tool calling still match the requests they were made for."""
    request = ModelRequest(messages=[Message(role="user", content="hi")], task="planning")
    before = {
        "task": "planning",
        "system": None,
        "messages": [{"role": "user", "content": "hi"}],
        "output_schema": None,
        "output_name": None,
        "max_tokens": 16000,
        "temperature": None,
        "data_class": "internal",
    }
    assert request_hash(request) == request_hash(before)
    auto = ModelRequest(messages=CONVERSATION, tools=[SEARCH], tool_choice="auto", max_tokens=50)
    assert request_hash(_request()) != request_hash(auto)


# ---------------------------------------------------------------------- through the kernel


class Asks(Provider):
    """Asks for one tool call, then answers."""

    name = "anthropic"

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(self, entry: ModelEntry, request: ModelRequest) -> Completion:
        self.requests.append(request)
        if request.messages[-1].role == "tool":
            return Completion(
                text="Refunds take 30 days.", input_tokens=20, output_tokens=6, model=entry.model
            )
        return Completion(
            text="",
            input_tokens=10,
            output_tokens=4,
            model=entry.model,
            stop_reason="tool_use",
            tool_calls=(
                ModelToolCall(id="toolu_1", name="kb__article__find", arguments={"q": "refunds"}),
            ),
        )


@pytest.fixture
async def bus() -> AsyncIterator[InMemoryEventBus]:
    async with InMemoryEventBus() as bus:
        yield bus


def _site():  # type: ignore[no-untyped-def]
    from aios.core.ports import CallSite  # noqa: PLC0415

    event = task_request("planning", step="plan")
    return CallSite(
        event=event,
        agent="planner-agent",
        principal="agent:planner-agent@acme",
        index=0,
        step="plan",
    )


async def test_the_kernel_records_what_the_model_asked_for(
    bus: InMemoryEventBus, tmp_path: Path
) -> None:
    request = ModelRequest(task="planning", messages=CONVERSATION[:1], tools=[SEARCH])
    cassette = Cassette(tmp_path / "cassette.json", "record")
    kernel = ModelKernel(bus, providers={"anthropic": Asks()}, cassette=cassette)
    response = await kernel.generate(request, _site())
    assert response.tool_calls == [
        ModelToolCall(id="toolu_1", name="kb__article__find", arguments={"q": "refunds"})
    ]
    [stored] = [s async for s in bus.replay(Scope("acme", "ai-researcher"), "model.call.completed")]
    assert ModelCallCompleted.model_validate(stored.event.data).tool_calls == response.tool_calls
    cassette.save()
    [recorded] = json.loads((tmp_path / "cassette.json").read_text("utf-8"))["interactions"]
    assert recorded["response"]["tool_calls"][0]["name"] == "kb__article__find"

    async with InMemoryEventBus() as other:
        replayed = ModelKernel(other, cassette=Cassette(tmp_path / "cassette.json", "replay"))
        again = await replayed.generate(request, _site())
    assert again.tool_calls == response.tool_calls


async def test_a_recording_without_tool_calls_stays_as_it_was(
    bus: InMemoryEventBus, tmp_path: Path
) -> None:
    class Says(Provider):
        name = "anthropic"

        async def complete(self, entry: ModelEntry, request: ModelRequest) -> Completion:
            return Completion(text="hello", input_tokens=1, output_tokens=1, model=entry.model)

    cassette = Cassette(tmp_path / "cassette.json", "record")
    kernel = ModelKernel(bus, providers={"anthropic": Says()}, cassette=cassette)
    await kernel.generate(ModelRequest(task="planning", messages=CONVERSATION[:1]), _site())
    cassette.save()
    [recorded] = json.loads((tmp_path / "cassette.json").read_text("utf-8"))["interactions"]
    assert "tool_calls" not in recorded["response"]


# ---------------------------------------------------------------------- an agent's loop


async def _answers(ctx: Context, task: dict[str, Any]) -> dict[str, Any]:
    tools = await ctx.tools.describe("kb.article.find")
    messages = [Message(role="user", content=task["message"])]
    for _ in range(4):
        reply = await ctx.model.complete(messages, tools=tools)
        if not reply.tool_calls:
            return {"answer": reply.text}
        messages.append(Message(role="assistant", content=reply.text, tool_calls=reply.tool_calls))
        for call in reply.tool_calls:
            found = await ctx.tools.call(call)
            messages.append(Message(role="tool", tool_call_id=call.id, content=json.dumps(found)))
    return {"answer": "gave up"}


DESK = """
apiVersion: aios/v1alpha1
kind: App
metadata: {name: helpdesk, version: 1.0.0}
spec:
  entrypoints: [{type: chat, workflow: answer}]
  agents: [helper]
  workflows: [answer]
  policies: [articles]
---
apiVersion: aios/v1alpha1
kind: Agent
metadata: {name: helper, version: 1.0.0}
spec:
  runtime: {type: python, entrypoint: "tool_calling_desk:answers"}
  capabilities: [{name: help.answer}]
  model: {task: planning}
  tools: {allow: ["kb.>"]}
---
apiVersion: aios/v1alpha1
kind: Workflow
metadata: {name: answer, version: 1.0.0}
spec:
  output: "${answer.output}"
  steps:
    - {id: answer, type: task, capability: help.answer, input: {message: "${input.message}"}}
---
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: articles, version: 1.0.0}
spec:
  subject: {app: helpdesk}
  allow:
    - {action: tool.invoke, resource: "kb.>"}
"""


@pytest.fixture
async def desk(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[LocalRuntime, Asks, list[str]]]:
    module = ModuleType("tool_calling_desk")
    module.answers = _answers  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tool_calling_desk", module)
    model = Asks()
    asked: list[str] = []

    @tool("kb.article.find", side_effect=SideEffect.READ, description="Find help articles.")
    async def find(q: str) -> dict[str, Any]:
        asked.append(q)
        return {"days": 30}

    async with LocalRuntime(tenant="acme", providers={"anthropic": model}) as runtime:
        await runtime.runtime.register_tool_provider(NativeProvider("kb", [find]))
        await runtime.runtime.apply(load_manifests(DESK), tenant="acme")
        yield runtime, model, asked


async def test_an_agent_offers_tools_and_makes_the_calls_itself(
    desk: tuple[LocalRuntime, Asks, list[str]],
) -> None:
    runtime, model, asked = desk
    result = await (await runtime.chat("helpdesk", "How long do refunds take?")).wait(timeout=10)
    assert result.status == "completed", result.error
    assert result.output == {"answer": "Refunds take 30 days."}
    assert asked == ["refunds"]  # the tool ran once, through Tool OS
    first, second = model.requests
    [offered] = first.tools
    assert (offered.name, offered.description) == ("kb__article__find", "Find help articles.")
    assert offered.parameters["properties"]["q"]["type"] == "string"
    assert [m.role for m in second.messages] == ["user", "assistant", "tool"]
    types = [e.type for e in result.events]
    assert types.count("model.call.completed") == 2
    assert types.count("tool.call.completed") == 1


async def test_a_tool_the_agent_may_not_use_stays_unused(
    desk: tuple[LocalRuntime, Asks, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model asks; policy decides. A tool outside the agent's allow list is refused."""
    runtime, model, asked = desk

    async def wants_money(entry: ModelEntry, request: ModelRequest) -> Completion:
        model.requests.append(request)
        return Completion(
            text="",
            input_tokens=1,
            output_tokens=1,
            model=entry.model,
            tool_calls=(
                ModelToolCall(id="t", name="pay__refund__create", arguments={"amount": 5000}),
            ),
        )

    monkeypatch.setattr(model, "complete", wants_money)
    result = await (await runtime.chat("helpdesk", "Refund me")).wait(timeout=10)
    assert result.status == "failed"
    assert "pay.refund.create" in str(result.error)
    assert asked == []


class AsksToRefund(Provider):
    """Asks for a refund, which needs a person, then says it is done."""

    name = "anthropic"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, entry: ModelEntry, request: ModelRequest) -> Completion:
        self.calls += 1
        if request.messages[-1].role == "tool":
            return Completion(text="Refunded.", input_tokens=5, output_tokens=2, model=entry.model)
        call = ModelToolCall(id="toolu_9", name="kb__refund__create", arguments={"order": "1"})
        return Completion(
            text="", input_tokens=5, output_tokens=2, model=entry.model, tool_calls=(call,)
        )


async def test_a_tool_call_that_waits_for_a_person_picks_up_where_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The task runs again once the person answers; the model's earlier answer, tool calls and
    all, comes from the record, so the model isn't asked twice and the call is made once."""

    async def refunds(ctx: Context, task: dict[str, Any]) -> dict[str, Any]:
        tools = await ctx.tools.describe("kb.refund.create")
        messages = [Message(role="user", content=task["message"])]
        for _ in range(3):
            reply = await ctx.model.complete(messages, tools=tools)
            if not reply.tool_calls:
                return {"answer": reply.text}
            messages.append(
                Message(role="assistant", content=reply.text, tool_calls=reply.tool_calls)
            )
            for call in reply.tool_calls:
                done = await ctx.tools.call(call)
                messages.append(
                    Message(role="tool", tool_call_id=call.id, content=json.dumps(done))
                )
        return {"answer": "gave up"}

    module = ModuleType("tool_calling_desk")
    module.answers = refunds  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tool_calling_desk", module)
    refunded: list[str] = []

    @tool("kb.refund.create", side_effect=SideEffect.FINANCIAL, description="Refund an order.")
    async def refund(order: str) -> dict[str, Any]:
        refunded.append(order)
        return {"refunded": order}

    policy = DESK.replace(
        '  allow:\n    - {action: tool.invoke, resource: "kb.>"}\n',
        '  allow:\n    - {action: tool.invoke, resource: "kb.>"}\n'
        "  requireApproval:\n"
        '    - {action: tool.invoke, resource: kb.refund.create, approvers: ["role:lead"]}\n',
    )
    model = AsksToRefund()
    async with LocalRuntime(tenant="acme", providers={"anthropic": model}) as runtime:
        await runtime.runtime.register_tool_provider(NativeProvider("kb", [refund]))
        await runtime.runtime.apply(load_manifests(policy), tenant="acme")
        run = await runtime.chat("helpdesk", "Refund order 1")
        event = await run.wait_for_event("approval.requested", timeout=10)
        data: dict[str, Any] = event.data if isinstance(event.data, dict) else {}
        await runtime.approve(str(data["approval"]), by="user:lead@acme")
        result = await run.wait(timeout=10)
    assert result.status == "completed", result.error
    assert result.output == {"answer": "Refunded."}
    assert refunded == ["1"]
    assert model.calls == 2  # the first answer was replayed, not asked for again


async def test_claude_gets_the_answers_to_two_calls_in_one_turn() -> None:
    """The Messages API wants every answer to an assistant's calls in the next user turn."""
    seen: list[Any] = []
    body = {
        "id": "m",
        "type": "message",
        "role": "assistant",
        "model": "model",
        "content": [{"type": "text", "text": "Both done."}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    second = ModelToolCall(id="call_2", name="kb__article__find", arguments={"q": "returns"})
    messages = [
        Message(role="user", content="Refunds and returns?"),
        Message(role="assistant", tool_calls=[CALL, second]),
        Message(role="tool", tool_call_id="call_1", content="a"),
        Message(role="tool", tool_call_id="call_2", content="b"),
    ]
    claude = _provider(
        lambda c: AnthropicProvider(api_key="XXXXXX", http_client=c, max_retries=0), body, seen
    )
    await claude.complete(ENTRY, ModelRequest(messages=messages, tools=[SEARCH]))
    [sent] = seen
    assert [m["role"] for m in sent["messages"]] == ["user", "assistant", "user"]
    assert [b["tool_use_id"] for b in sent["messages"][2]["content"]] == ["call_1", "call_2"]
