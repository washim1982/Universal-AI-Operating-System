"""The Gemini adapter (FR-MOD-02), on the real ``google-genai`` SDK against a stand-in server."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest

from aios.core.envelope import DataClass
from aios.core.manifests import parse_manifest
from aios.core.ports import CallSite, EmbeddingRequest, Message, ModelRequest
from aios.fabric import InMemoryEventBus
from aios.models import (
    Completion,
    ModelEntry,
    ModelKernel,
    ModelRegistry,
    Provider,
    ProviderError,
)
from aios.models.gemini import GeminiProvider
from aios.testing import task_request

ENTRY = ModelEntry(id="google/gemini", model="gemini-test")
RESPONSE = {
    "candidates": [
        {
            "content": {"role": "model", "parts": [{"text": '{"answer": "42"}'}]},
            "finishReason": "STOP",
        }
    ],
    "modelVersion": "gemini-test-001",
    "usageMetadata": {
        "promptTokenCount": 900,
        "candidatesTokenCount": 30,
        "cachedContentTokenCount": 800,
        "totalTokenCount": 930,
    },
}


def _provider(
    body: dict[str, Any] | None = None,
    *,
    status: int = 200,
    seen: list[httpx2.Request] | None = None,
) -> GeminiProvider:
    def handle(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        return httpx2.Response(status, json=body if body is not None else RESPONSE)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
    return GeminiProvider(api_key="XXXXXX", http_client=client)


def _request(**fields: Any) -> ModelRequest:
    return ModelRequest(messages=[Message(role="user", content="hi")], **fields)


async def test_what_it_sends() -> None:
    seen: list[httpx2.Request] = []
    await _provider(seen=seen).complete(
        ENTRY,
        ModelRequest(
            system="Be careful.",
            messages=[
                Message(role="user", content="hi"),
                Message(role="assistant", content="hello"),
                Message(role="user", content="and then?"),
            ],
            max_tokens=100,
            temperature=0.2,
        ),
    )
    [sent] = seen
    assert "gemini-test:generateContent" in str(sent.url)
    body = json.loads(sent.content)
    assert [turn["role"] for turn in body["contents"]] == ["user", "model", "user"]
    assert body["contents"][1]["parts"] == [{"text": "hello"}]
    assert body["systemInstruction"]["parts"] == [{"text": "Be careful."}]
    assert body["generationConfig"]["maxOutputTokens"] == 100
    assert body["generationConfig"]["temperature"] == 0.2


async def test_what_it_sends_for_structured_output() -> None:
    seen: list[httpx2.Request] = []
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    await _provider(seen=seen).complete(ENTRY, _request(output_schema=schema))
    config = json.loads(seen[0].content)["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"] == schema


async def test_what_it_reads_back() -> None:
    done = await _provider().complete(ENTRY, _request())
    assert done.text == '{"answer": "42"}'
    assert (done.input_tokens, done.output_tokens) == (900, 30)
    assert done.cache_read_tokens == 800
    assert done.model == "gemini-test-001"
    assert done.stop_reason == "STOP"


@pytest.mark.parametrize(
    "body",
    [
        {"promptFeedback": {"blockReason": "SAFETY"}},
        {"candidates": [{"finishReason": "SAFETY"}]},
        {"candidates": [{"finishReason": "PROHIBITED_CONTENT"}]},
    ],
)
async def test_a_refusal_moves_to_the_next_candidate(body: dict[str, Any]) -> None:
    with pytest.raises(ProviderError, match="declined") as raised:
        await _provider(body).complete(ENTRY, _request())
    assert not raised.value.retryable


@pytest.mark.parametrize(
    ("status", "retryable"), [(400, False), (403, False), (429, True), (500, True), (503, True)]
)
async def test_errors_say_whether_to_try_again(status: int, retryable: bool) -> None:
    body = {"error": {"code": status, "message": "no", "status": "X"}}
    with pytest.raises(ProviderError) as raised:
        await _provider(body, status=status).complete(ENTRY, _request())
    assert raised.value.retryable is retryable


async def test_a_dropped_connection_is_worth_another_try() -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
    provider = GeminiProvider(api_key="XXXXXX", http_client=client)
    with pytest.raises(ProviderError, match="connection error") as raised:
        await provider.complete(ENTRY, _request())
    assert raised.value.retryable


async def test_embeddings() -> None:
    seen: list[httpx2.Request] = []
    body = {"embeddings": [{"values": [0.1, 0.2]}, {"values": [0.3, 0.4]}]}
    done = await _provider(body, seen=seen).embed(
        ENTRY, EmbeddingRequest(texts=["twelve chars", "twelve chars"])
    )
    assert done.vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert done.input_tokens == 6


async def test_a_short_answer_to_an_embedding_is_an_error() -> None:
    body = {"embeddings": [{"values": [0.1]}]}
    with pytest.raises(ProviderError, match="1 vectors for 2 texts"):
        await _provider(body).embed(ENTRY, EmbeddingRequest(texts=["a", "b"]))


def test_no_key_is_a_failure_the_next_candidate_can_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ProviderError) as raised:
        GeminiProvider()
    assert not raised.value.retryable


@pytest.fixture
async def bus() -> AsyncIterator[InMemoryEventBus]:
    async with InMemoryEventBus() as bus:
        yield bus


def _policy() -> Any:
    return parse_manifest(
        {
            "apiVersion": "aios/v1alpha1",
            "kind": "ModelPolicy",
            "metadata": {"name": "default", "version": "1.0.0"},
            "spec": {"default": {"candidates": ["google/gemini", "fallback/ok"]}},
        }
    )


def _site() -> CallSite:
    event = task_request("planning", step="plan")
    return CallSite(
        event=event, agent="planner-agent", principal="agent:x@acme", index=0, step="plan"
    )


class Standin(Provider):
    name = "google"

    async def complete(self, entry: ModelEntry, request: ModelRequest) -> Completion:
        return Completion(text="ok", input_tokens=1, output_tokens=1, model=entry.model)


async def test_the_kernel_finds_the_adapter_by_itself(
    bus: InMemoryEventBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("aios.models.gemini.GeminiProvider", Standin)
    kernel = ModelKernel(
        bus, registry=ModelRegistry([ENTRY]), policies={"default": _policy()}, providers={}
    )
    response = await kernel.generate(_request(), _site())
    assert (response.provider, response.text) == ("google", "ok")


async def test_with_no_key_the_next_candidate_answers(
    bus: InMemoryEventBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    class Fallback(Standin):
        name = "fallback"

    kernel = ModelKernel(
        bus,
        registry=ModelRegistry([ENTRY, ModelEntry(id="fallback/ok", model="ok")]),
        policies={"default": _policy()},
        providers={"fallback": Fallback()},
    )
    response = await kernel.generate(_request(), _site())
    assert response.provider == "fallback"


async def test_a_real_call_through_the_kernel(bus: InMemoryEventBus) -> None:
    kernel = ModelKernel(
        bus,
        registry=ModelRegistry([ENTRY]),
        policies={"default": _policy()},
        providers={"google": _provider()},
    )
    response = await kernel.generate(_request(data_class=DataClass.INTERNAL), _site())
    assert response.provider == "google"
    assert response.usage.cache_read_tokens == 800
