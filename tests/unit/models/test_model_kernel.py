import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest

from aios.core.cassettes import Cassette, CassetteMissError
from aios.core.envelope import DataClass, Event
from aios.core.errors import ModelError
from aios.core.manifests import ModelPolicyManifest, parse_manifest
from aios.core.ports import CallSite, Message, ModelRequest
from aios.fabric import InMemoryEventBus, Scope
from aios.models import (
    AnthropicProvider,
    Completion,
    ModelEntry,
    ModelKernel,
    ModelRegistry,
    OpenAIProvider,
    Provider,
    ProviderError,
    Router,
    default_policy,
)
from aios.testing import task_request

SCOPE = Scope("acme", "ai-researcher")


class FakeProvider(Provider):
    def __init__(self, name: str, *errors: ProviderError) -> None:
        self.name = name
        self.errors = list(errors)
        self.calls: list[str] = []

    async def complete(self, entry: ModelEntry, request: ModelRequest) -> Completion:
        self.calls.append(entry.id)
        if self.errors:
            raise self.errors.pop(0)
        return Completion(
            text='{"ok": true}', input_tokens=1_000, output_tokens=500, model=entry.model
        )


def _request(task: str | None = "planning", **fields: Any) -> ModelRequest:
    return ModelRequest(task=task, messages=[Message(role="user", content="hi")], **fields)


def _site(event: Event | None = None, index: int = 0) -> CallSite:
    event = event or task_request("planning", step="plan")
    return CallSite(
        event=event,
        agent="planner-agent",
        principal="agent:planner-agent@acme",
        index=index,
        step="plan",
    )


def _policy(rules: list[dict[str, Any]], default: list[str]) -> ModelPolicyManifest:
    manifest = parse_manifest(
        {
            "apiVersion": "aios/v1alpha1",
            "kind": "ModelPolicy",
            "metadata": {"name": "default", "version": "1.0.0"},
            "spec": {"rules": rules, "default": {"candidates": default}, "retries": {"max": 1}},
        }
    )
    assert isinstance(manifest, ModelPolicyManifest)
    return manifest


@pytest.fixture
async def bus() -> AsyncIterator[InMemoryEventBus]:
    async with InMemoryEventBus() as bus:
        yield bus


# --------------------------------------------------------------------------- registry, router


def test_registry_prices_and_long_context_tiers() -> None:
    registry = ModelRegistry.default()
    opus = registry.get("anthropic/claude-opus-5-5")
    haiku = registry.get("anthropic/claude-haiku-5-5")
    assert opus is not None
    assert haiku is not None
    assert opus.cost(1_000_000, 1_000_000) == pytest.approx(24.0)
    assert haiku.cost(100_000, 0) == pytest.approx(0.01)
    assert haiku.cost(200_000, 0) == pytest.approx(0.10)  # long-context rate above 100K


def test_router_applies_rules_then_the_default() -> None:
    router = Router(ModelRegistry.default())
    policy = default_policy()
    reasoning = router.route(policy, _request("reasoning"))
    assert [c.id for c in reasoning.candidates] == ["anthropic/claude-opus-5-5"]
    assert reasoning.rule == "rules[2]"
    assert reasoning.skipped == ("openai/<reasoning-model>",)  # placeholder, live mode
    other = router.route(policy, _request("planning"))
    assert [c.id for c in other.candidates] == ["anthropic/claude-sonnet-5-5"]
    replay = router.route(policy, _request("planning"), allow_placeholders=True)
    assert [c.id for c in replay.candidates] == [
        "anthropic/claude-sonnet-5-5",
        "google/<general-model>",
    ]


def test_restricted_data_only_goes_to_private_deployments() -> None:
    router = Router(ModelRegistry.default())
    policy = _policy([], ["anthropic/claude-sonnet-5-5", "private/<small-model>"])
    route = router.route(policy, _request(data_class=DataClass.RESTRICTED), allow_placeholders=True)
    assert [c.id for c in route.candidates] == ["private/<small-model>"]
    assert route.skipped == ("anthropic/claude-sonnet-5-5",)


# --------------------------------------------------------------------------- kernel


async def test_records_the_call_with_tokens_and_cost(bus: InMemoryEventBus) -> None:
    anthropic = FakeProvider("anthropic")
    kernel = ModelKernel(bus, providers={"anthropic": anthropic})
    site = _site()
    response = await kernel.generate(_request(output_schema={"type": "object"}), site)
    assert (response.provider, response.model, response.parsed) == (
        "anthropic",
        "claude-sonnet-5-5",
        {"ok": True},
    )
    assert response.usage.cost_usd == pytest.approx(0.007)  # 1K in at $2/M + 500 out at $10/M
    [stored] = [s async for s in bus.replay(SCOPE, "model.call.completed")]
    event = stored.event
    assert event.causationid == site.event.id
    assert event.principal == "agent:planner-agent@acme"
    assert isinstance(event.data, dict)
    usage = event.data["usage"]
    assert isinstance(usage, dict)
    assert usage["cost_usd"] == pytest.approx(0.007)
    assert event.data["output_text"] == '{"ok": true}'
    assert event.data["site"] == "plan/planner-agent/l0a1/model/0"


async def test_retries_then_falls_back_to_the_next_candidate(bus: InMemoryEventBus) -> None:
    flaky = FakeProvider(
        "anthropic",
        ProviderError("429", retryable=True),
        ProviderError("400", retryable=False),
    )
    openai = FakeProvider("openai")
    kernel = ModelKernel(
        bus,
        providers={"anthropic": flaky, "openai": openai},
        retry_delay=0,
        policies={"default": _policy([], ["anthropic/claude-sonnet-5-5", "openai/gpt-x"])},
    )
    kernel.registry.add(ModelEntry(id="openai/gpt-x", model="gpt-x"))
    response = await kernel.generate(_request(), _site())
    assert flaky.calls == ["anthropic/claude-sonnet-5-5"] * 2
    assert response.provider == "openai"
    [stored] = [s async for s in bus.replay(SCOPE, "model.call.completed")]
    assert isinstance(stored.event.data, dict)
    assert stored.event.data["fallback_from"] == ["anthropic/claude-sonnet-5-5"]


async def test_a_call_fails_when_every_candidate_fails(bus: InMemoryEventBus) -> None:
    broken = FakeProvider("anthropic", ProviderError("500", retryable=False))
    kernel = ModelKernel(bus, providers={"anthropic": broken})
    with pytest.raises(ModelError, match="every candidate failed"):
        await kernel.generate(_request(), _site())
    assert [s.event.type async for s in bus.replay(SCOPE)] == ["model.call.failed"]


async def test_a_completed_call_is_reused_not_repeated(bus: InMemoryEventBus) -> None:
    provider = FakeProvider("anthropic")
    kernel = ModelKernel(bus, providers={"anthropic": provider})
    task = task_request("planning", step="plan")
    await bus.publish(task)
    first = await kernel.generate(_request(), _site(task))
    again = await kernel.generate(_request(), _site(task))  # the task re-executes after a crash
    assert provider.calls == ["anthropic/claude-sonnet-5-5"]
    assert again.replayed
    assert again.text == first.text
    other = await kernel.generate(_request(), _site(task, index=1))
    assert not other.replayed


async def test_cassettes_record_and_replay_by_call_site(
    bus: InMemoryEventBus, tmp_path: Path
) -> None:
    recorder = Cassette(tmp_path / "cassette.json", mode="record")
    provider = FakeProvider("anthropic")
    await ModelKernel(bus, providers={"anthropic": provider}, cassette=recorder).generate(
        _request(), _site()
    )
    recorder.save()

    async with InMemoryEventBus() as fresh:
        player = Cassette.open(tmp_path, mode="replay")
        policy = _policy(
            [{"match": {"task": "planning"}, "candidates": ["google/<general-model>"]}],
            ["anthropic/claude-sonnet-5-5"],
        )
        kernel = ModelKernel(fresh, policies={"default": policy}, cassette=player)
        response = await kernel.generate(_request(), _site())
        assert (response.provider, response.text) == ("google", '{"ok": true}')  # rerouted
        assert player.repeated_calls == []
        with pytest.raises(CassetteMissError):
            await kernel.generate(_request(), _site(index=1))


async def test_a_placeholder_cannot_be_called_live(bus: InMemoryEventBus) -> None:
    policy = _policy([], ["google/<general-model>"])
    kernel = ModelKernel(bus, policies={"default": policy})
    with pytest.raises(ModelError, match="no usable candidate"):
        await kernel.generate(_request(), _site())


# --------------------------------------------------------------------------- adapters


def _transport(body: dict[str, Any], requests: list[httpx2.Request]) -> httpx2.MockTransport:
    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=body)

    return httpx2.MockTransport(handle)


ANTHROPIC_MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-5-5",
    "content": [{"type": "text", "text": '{"answer": "42"}'}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 80},
}


async def test_anthropic_adapter_request_and_response() -> None:
    requests: list[httpx2.Request] = []
    client = httpx2.AsyncClient(transport=_transport(ANTHROPIC_MESSAGE, requests))
    provider = AnthropicProvider(api_key="XXXXXX", http_client=client, max_retries=0)
    entry = ModelRegistry.default().get("anthropic/claude-opus-5-5")
    assert entry is not None
    request = _request(
        system="Be careful.",
        temperature=0.2,
        output_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
    )
    completion = await provider.complete(entry, request)
    assert (completion.text, completion.input_tokens, completion.output_tokens) == (
        '{"answer": "42"}',
        200,
        30,
    )
    [sent] = requests
    body = json.loads(sent.content)
    assert body["model"] == "claude-opus-5-5"
    assert body["system"] == "Be careful."
    assert "temperature" not in body  # rejected by the Messages API since 2026-10-21
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in sent.headers["anthropic-beta"]


async def test_anthropic_refusals_move_to_the_next_candidate() -> None:
    refused = {**ANTHROPIC_MESSAGE, "stop_reason": "refusal", "content": []}
    client = httpx2.AsyncClient(transport=_transport(refused, []))
    provider = AnthropicProvider(api_key="XXXXXX", http_client=client, max_retries=0)
    entry = ModelEntry(id="anthropic/claude-haiku-5-5", model="claude-haiku-5-5")
    with pytest.raises(ProviderError, match="declined") as raised:
        await provider.complete(entry, _request())
    assert not raised.value.retryable


async def test_openai_compatible_adapter() -> None:
    requests: list[httpx2.Request] = []
    body = {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "llama-4",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "hello"},
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
    }
    client = httpx2.AsyncClient(transport=_transport(body, requests))
    provider = OpenAIProvider(
        name="private", base_url="http://vllm.local/v1", http_client=client, max_retries=0
    )
    entry = ModelEntry(id="private/llama-4", model="llama-4", deployment="private")
    completion = await provider.complete(
        entry, _request(system="sys", output_schema={"type": "object"}, temperature=0.1)
    )
    assert (completion.text, completion.input_tokens, completion.output_tokens) == ("hello", 7, 2)
    sent = json.loads(requests[0].content)
    assert sent["messages"][0] == {"role": "system", "content": "sys"}
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["temperature"] == 0.1
    assert str(requests[0].url) == "http://vllm.local/v1/chat/completions"
