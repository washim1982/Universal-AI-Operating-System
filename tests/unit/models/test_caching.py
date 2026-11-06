"""Provider prompt caching (FR-MOD-09): ask for it where it pays, and count what it did."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest

from aios.core.cassettes import Cassette
from aios.core.catalog import ModelCallCompleted
from aios.core.envelope import DataClass
from aios.core.ports import CallSite, Message, ModelRequest
from aios.fabric import InMemoryEventBus, Scope
from aios.models import (
    AnthropicProvider,
    Completion,
    ModelEntry,
    ModelKernel,
    ModelRegistry,
    OpenAIProvider,
    Prices,
    Provider,
)
from aios.observability import EventMetrics
from aios.testing import task_request

SCOPE = Scope("acme", "ai-researcher")
OPUS = ModelEntry(id="anthropic/opus", model="claude-opus-5-5")
PRICED = ModelEntry(
    id="fake/priced",
    model="priced",
    prices=Prices(input=4.0, output=20.0, cacheRead=0.2, cacheWrite=5.0),
)


def _site() -> CallSite:
    event = task_request("planning", step="plan")
    return CallSite(
        event=event, agent="planner-agent", principal="agent:x@acme", index=0, step="plan"
    )


def _messages(*texts: str) -> list[Message]:
    roles = ("user", "assistant")
    return [Message(role=roles[i % 2], content=text) for i, text in enumerate(texts)]  # type: ignore[arg-type]


@pytest.fixture
async def bus() -> AsyncIterator[InMemoryEventBus]:
    async with InMemoryEventBus() as bus:
        yield bus


# --------------------------------------------------------------------------- asking for it

MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-5-5",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 100,
        "output_tokens": 30,
        "cache_read_input_tokens": 800,
        "cache_creation_input_tokens": 50,
    },
}


async def _sent(request: ModelRequest) -> dict[str, Any]:
    seen: list[httpx2.Request] = []

    def handle(sent: httpx2.Request) -> httpx2.Response:
        seen.append(sent)
        return httpx2.Response(200, json=MESSAGE)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
    provider = AnthropicProvider(api_key="XXXXXX", http_client=client, max_retries=0)
    await provider.complete(OPUS, request)
    return json.loads(seen[0].content)


async def test_a_system_prompt_is_worth_keeping() -> None:
    body = await _sent(ModelRequest(system="Be careful.", messages=_messages("hi")))
    assert body["system"] == [
        {"type": "text", "text": "Be careful.", "cache_control": {"type": "ephemeral"}}
    ]


async def test_one_question_isnt() -> None:
    """It won't come back, so marking it would only pay the write premium."""
    body = await _sent(ModelRequest(system="Be careful.", messages=_messages("hi")))
    assert "cache_control" not in body
    assert "cache_control" not in body["messages"][0]


async def test_a_conversation_is_worth_keeping() -> None:
    body = await _sent(ModelRequest(messages=_messages("hi", "hello", "and then?")))
    assert body["cache_control"] == {"type": "ephemeral"}


async def test_no_system_prompt_nothing_to_mark() -> None:
    body = await _sent(ModelRequest(messages=_messages("hi")))
    assert "cache_control" not in body
    assert "system" not in body


async def test_restricted_data_is_never_offered_to_a_cache() -> None:
    body = await _sent(
        ModelRequest(
            system="Be careful.",
            messages=_messages("hi", "hello", "and then?"),
            data_class=DataClass.RESTRICTED,
        )
    )
    assert body["system"] == "Be careful."
    assert "cache_control" not in body


async def test_the_kernel_tells_the_adapter_what_was_really_read(
    bus: InMemoryEventBus,
) -> None:
    """A request can understate its label, and the cache must hear the strictest one."""
    seen: list[DataClass] = []

    class Spy(Provider):
        name = "fake"

        async def complete(self, entry: ModelEntry, request: ModelRequest) -> Completion:
            seen.append(request.data_class)
            return Completion(text="ok", input_tokens=1, output_tokens=1, model=entry.model)

    private = ModelEntry(id="fake/ours", model="ours", deployment="private")
    kernel = ModelKernel(
        bus,
        registry=ModelRegistry([private]),
        providers={"fake": Spy()},
        policies={"default": _only(private.id)},
    )
    site = CallSite(
        event=task_request("planning", step="plan"),
        agent="planner-agent",
        principal="agent:x@acme",
        index=0,
        data_class=DataClass.RESTRICTED,
    )
    await kernel.generate(ModelRequest(messages=_messages("hi")), site)
    assert seen == [DataClass.RESTRICTED]


def _only(model: str) -> Any:
    from aios.core.manifests import parse_manifest  # noqa: PLC0415

    return parse_manifest(
        {
            "apiVersion": "aios/v1alpha1",
            "kind": "ModelPolicy",
            "metadata": {"name": "default", "version": "1.0.0"},
            "spec": {"default": {"candidates": [model]}},
        }
    )


# --------------------------------------------------------------------------- counting it


async def test_anthropic_says_what_was_cached() -> None:
    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, json=MESSAGE))
    )
    provider = AnthropicProvider(api_key="XXXXXX", http_client=client, max_retries=0)
    done = await provider.complete(OPUS, ModelRequest(messages=_messages("hi")))
    assert done.input_tokens == 950  # the whole prompt
    assert (done.cache_read_tokens, done.cache_write_tokens) == (800, 50)


async def test_openai_says_what_was_cached() -> None:
    body = {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
        ],
        "usage": {
            "prompt_tokens": 900,
            "completion_tokens": 2,
            "total_tokens": 902,
            "prompt_tokens_details": {"cached_tokens": 800},
        },
    }
    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, json=body))
    )
    provider = OpenAIProvider(api_key="XXXXXX", http_client=client, max_retries=0)
    entry = ModelEntry(id="openai/gpt", model="gpt")
    done = await provider.complete(entry, ModelRequest(messages=_messages("hi")))
    assert (done.input_tokens, done.cache_read_tokens) == (900, 800)


def test_cached_tokens_cost_what_their_prices_say() -> None:
    plain = PRICED.cost(1_000_000, 0)
    assert plain == pytest.approx(4.0)
    assert PRICED.cost(1_000_000, 0, cache_read=1_000_000) == pytest.approx(0.2)
    assert PRICED.cost(1_000_000, 0, cache_write=1_000_000) == pytest.approx(5.0)
    mixed = PRICED.cost(1_000_000, 1_000, cache_read=500_000, cache_write=250_000)
    assert mixed == pytest.approx(1.0 + 0.1 + 1.25 + 0.02)


def test_a_model_with_no_cache_price_charges_the_input_price() -> None:
    entry = ModelEntry(id="fake/plain", model="plain", prices=Prices(input=4.0, output=20.0))
    assert entry.cost(1_000_000, 0, cache_read=500_000) == pytest.approx(4.0)


def test_a_zero_price_isnt_mistaken_for_no_price() -> None:
    entry = ModelEntry(
        id="fake/free", model="free", prices=Prices(input=4.0, output=0.0, cacheRead=0.0)
    )
    assert entry.cost(1_000_000, 0, cache_read=1_000_000) == 0.0


def test_a_cache_count_can_never_make_the_rest_negative() -> None:
    assert PRICED.cost(100, 0, cache_read=1_000_000) >= 0


def test_the_shipped_prices_know_about_caching() -> None:
    registry = ModelRegistry.default()
    for name in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"):
        entry = registry.get(f"anthropic/{name}")
        assert entry is not None
        assert entry.prices is not None
        assert entry.prices.cache_read is not None
        assert entry.prices.cache_write is not None
        assert entry.prices.cache_read < entry.prices.input < entry.prices.cache_write


class Cached(Provider):
    name = "fake"

    async def complete(self, entry: ModelEntry, request: ModelRequest) -> Completion:
        return Completion(
            text="ok",
            input_tokens=1_000_000,
            output_tokens=0,
            model=entry.model,
            cache_read_tokens=800_000,
            cache_write_tokens=100_000,
        )


async def test_the_record_says_what_was_cached_and_what_it_cost(bus: InMemoryEventBus) -> None:
    kernel = ModelKernel(
        bus,
        registry=ModelRegistry([PRICED]),
        providers={"fake": Cached()},
        policies={"default": _only(PRICED.id)},
    )
    response = await kernel.generate(ModelRequest(messages=_messages("hi")), _site())
    assert response.usage.cost_usd == pytest.approx(0.4 + 0.16 + 0.5)
    [stored] = [s.event async for s in bus.replay(SCOPE, "model.call.completed")]
    usage = ModelCallCompleted.model_validate(stored.data).usage
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (800_000, 100_000)


async def test_a_cassette_keeps_what_was_cached(bus: InMemoryEventBus, tmp_path: Path) -> None:
    path = tmp_path / "cassette.json"
    site = _site()
    request = ModelRequest(messages=_messages("hi"))
    recording = Cassette(path, "record")
    kernel = ModelKernel(
        bus,
        registry=ModelRegistry([PRICED]),
        providers={"fake": Cached()},
        policies={"default": _only(PRICED.id)},
        cassette=recording,
    )
    await kernel.generate(request, site)
    recording.save()
    async with InMemoryEventBus() as replay:
        kernel = ModelKernel(
            replay,
            registry=ModelRegistry([PRICED]),
            policies={"default": _only(PRICED.id)},
            cassette=Cassette(path, "replay"),
        )
        response = await kernel.generate(request, site)
    assert response.usage.cache_read_tokens == 800_000
    assert response.usage.cache_write_tokens == 100_000


async def test_cache_hits_show_up_in_the_metrics(bus: InMemoryEventBus) -> None:
    metrics = EventMetrics(bus)
    kernel = ModelKernel(
        bus,
        registry=ModelRegistry([PRICED]),
        providers={"fake": Cached()},
        policies={"default": _only(PRICED.id)},
    )
    await kernel.generate(ModelRequest(messages=_messages("hi")), _site())
    [stored] = [s.event async for s in bus.replay(SCOPE, "model.call.completed")]
    metrics.observe(stored)
    text = metrics.exposition().decode()
    read = (
        'aios_model_cache_tokens_total{app="ai-researcher",direction="read",model="priced",'
        'provider="fake",tenant="acme"} 800000.0'
    )
    assert read in text
    assert 'direction="write"' in text
