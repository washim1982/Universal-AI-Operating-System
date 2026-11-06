"""What every provider adapter must do, against a stand-in for each provider's API (ADR-008)."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest

from aios.core.ports import Message, ModelRequest
from aios.models import AnthropicProvider, ModelEntry, OpenAIProvider, Provider, ProviderError
from aios.models.gemini import GeminiProvider

ENTRY = ModelEntry(id="any/model", model="model")
Handler = Callable[[httpx2.Request], httpx2.Response]


@dataclass(frozen=True)
class Adapter:
    make: Callable[[httpx2.AsyncClient], Provider]
    body: dict[str, Any]


ADAPTERS = {
    "anthropic": Adapter(
        lambda client: AnthropicProvider(api_key="XXXXXX", http_client=client, max_retries=0),
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "model",
            "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 7, "output_tokens": 2},
        },
    ),
    "openai": Adapter(
        lambda client: OpenAIProvider(api_key="XXXXXX", http_client=client, max_retries=0),
        {
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": "model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "hello"},
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
        },
    ),
    "google": Adapter(
        lambda client: GeminiProvider(api_key="XXXXXX", http_client=client),
        {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "hello"}]}, "finishReason": "STOP"}
            ],
            "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2},
        },
    ),
}


def _provider(adapter: Adapter, handle: Handler) -> Provider:
    return adapter.make(httpx2.AsyncClient(transport=httpx2.MockTransport(handle)))


def _request() -> ModelRequest:
    return ModelRequest(
        system="Be careful.",
        messages=[Message(role="user", content="hi")],
        max_tokens=50,
    )


@pytest.fixture(params=list(ADAPTERS))
def adapter(request: pytest.FixtureRequest) -> Adapter:
    return ADAPTERS[request.param]


async def test_it_answers_and_counts_tokens(adapter: Adapter) -> None:
    provider = _provider(adapter, lambda _: httpx2.Response(200, json=adapter.body))
    done = await provider.complete(ENTRY, _request())
    assert done.text == "hello"
    assert (done.input_tokens, done.output_tokens) == (7, 2)
    assert done.model
    assert done.latency_ms >= 0


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_a_failure_that_may_pass_is_retryable(adapter: Adapter, status: int) -> None:
    error = {"error": {"type": "api_error", "code": status, "message": "busy", "status": "X"}}
    provider = _provider(adapter, lambda _: httpx2.Response(status, json=error))
    with pytest.raises(ProviderError) as raised:
        await provider.complete(ENTRY, _request())
    assert raised.value.retryable is True


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_a_failure_that_wont_pass_isnt(adapter: Adapter, status: int) -> None:
    error = {"error": {"type": "bad", "code": status, "message": "no", "status": "X"}}
    provider = _provider(adapter, lambda _: httpx2.Response(status, json=error))
    with pytest.raises(ProviderError) as raised:
        await provider.complete(ENTRY, _request())
    assert raised.value.retryable is False


async def test_a_lost_connection_is_retryable(adapter: Adapter) -> None:
    def lost(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(ProviderError) as raised:
        await _provider(adapter, lost).complete(ENTRY, _request())
    assert raised.value.retryable is True
