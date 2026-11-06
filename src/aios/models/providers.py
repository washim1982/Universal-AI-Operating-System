"""Provider adapters over the official SDKs (ADR-008, FR-MOD-02).

Each adapter turns a provider-neutral :class:`ModelRequest` into one provider call and returns
a :class:`Completion`: the text, the token counts and the model that actually served it.
Routing, fallback, accounting and recording stay in the kernel. Providers with an embeddings
API also return an :class:`Embedding`; the others refuse, and the kernel tries the next candidate.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Any, cast

import anthropic
import httpx2
import openai

from aios.core.envelope import DataClass
from aios.core.errors import ModelError
from aios.core.ports import EmbeddingRequest, ModelRequest
from aios.models.registry import ModelEntry

ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"
EPHEMERAL = {"type": "ephemeral"}


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    stop_reason: str | None = None
    latency_ms: float = 0.0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True, slots=True)
class Embedding:
    vectors: list[list[float]]
    input_tokens: int
    model: str
    latency_ms: float = 0.0


class ProviderError(ModelError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class Provider(ABC):
    name: str

    @abstractmethod
    async def complete(self, entry: ModelEntry, request: ModelRequest) -> Completion: ...

    async def embed(self, entry: ModelEntry, request: EmbeddingRequest) -> Embedding:
        raise ProviderError(f"{self.name} has no embeddings API", retryable=False)


class AnthropicProvider(Provider):
    """Claude through the official ``anthropic`` SDK (Messages API)."""

    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 2,
        timeout: float = 600.0,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self.client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            max_retries=max_retries,
            timeout=timeout,
            http_client=http_client,
        )

    async def complete(self, entry: ModelEntry, request: ModelRequest) -> Completion:
        # No sampling parameters: the Messages API rejects temperature/top_p/top_k since
        # 2026-10-21. Thinking is left at the model default (adaptive on the 5.5 family).
        params: dict[str, Any] = {
            "model": entry.model,
            "max_tokens": request.max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        if request.system:
            params["system"] = request.system
        if request.data_class is not DataClass.RESTRICTED:
            self._cache(params, request)
        if request.output_schema is not None:
            params["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": anthropic.transform_schema(request.output_schema),
                }
            }
        started = time.perf_counter()
        try:
            if entry.refusal_fallback:
                # On a policy refusal the API re-runs the request on a fallback model itself.
                message: Any = cast(
                    Any,
                    await self.client.beta.messages.create(
                        **params, betas=[ANTHROPIC_FALLBACK_BETA], fallbacks="default"
                    ),
                )
            else:
                message = cast(Any, await self.client.messages.create(**params))
        except anthropic.APIStatusError as exc:
            raise ProviderError(
                f"anthropic {exc.status_code}: {exc.message}",
                retryable=exc.status_code in (408, 409, 429) or exc.status_code >= 500,
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"anthropic connection error: {exc}", retryable=True) from exc
        latency = (time.perf_counter() - started) * 1000
        if message.stop_reason == "refusal":
            raise ProviderError(f"{message.model} declined the request", retryable=False)
        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        )
        usage = message.usage
        cache_write = getattr(usage, "cache_creation_input_tokens", None) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
        return Completion(
            text=text,
            input_tokens=usage.input_tokens + cache_write + cache_read,
            output_tokens=usage.output_tokens,
            model=str(message.model),
            stop_reason=message.stop_reason,
            latency_ms=latency,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

    @staticmethod
    def _cache(params: dict[str, Any], request: ModelRequest) -> None:
        """Mark what repeats (FR-MOD-09): the system prompt, and a conversation as it grows.

        One question isn't marked: it won't come back, so it would pay the write premium for
        nothing. A cache holds a prompt for a while, so restricted data is never offered to one.
        """
        if request.system:
            params["system"] = [
                {"type": "text", "text": request.system, "cache_control": EPHEMERAL}
            ]
        if len(request.messages) > 1:
            params["cache_control"] = EPHEMERAL


class OpenAIProvider(Provider):
    """OpenAI, or any OpenAI-compatible server (vLLM, Ollama, llama.cpp, ...) with ``base_url``.

    ``extra_body`` adds server-specific fields to every request.
    """

    def __init__(
        self,
        *,
        name: str = "openai",
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 2,
        timeout: float = 600.0,
        extra_body: Mapping[str, Any] | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.extra_body = dict(extra_body or {})
        self.client = openai.AsyncOpenAI(
            api_key=api_key or ("unused" if base_url else None),
            base_url=base_url,
            max_retries=max_retries,
            timeout=timeout,
            http_client=http_client,
        )

    async def complete(self, entry: ModelEntry, request: ModelRequest) -> Completion:
        messages: list[dict[str, str]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages += [{"role": m.role, "content": m.content} for m in request.messages]
        params: dict[str, Any] = {
            "model": entry.model,
            "messages": messages,
            "max_completion_tokens": request.max_tokens,
        }
        if request.temperature is not None:
            params["temperature"] = request.temperature
        if request.output_schema is not None:
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.output_name or "output",
                    "schema": request.output_schema,
                    "strict": False,  # the kernel validates the output itself
                },
            }
        if self.extra_body:
            params["extra_body"] = self.extra_body
        started = time.perf_counter()
        response: Any = await self._sent(cast(Any, self.client.chat.completions.create(**params)))
        latency = (time.perf_counter() - started) * 1000
        choice = response.choices[0]
        usage = response.usage
        details = getattr(usage, "prompt_tokens_details", None)
        return Completion(
            text=cast(str, choice.message.content or ""),
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            model=str(response.model),
            stop_reason=choice.finish_reason,
            latency_ms=latency,
            cache_read_tokens=getattr(details, "cached_tokens", None) or 0,
            cache_write_tokens=getattr(details, "cache_write_tokens", None) or 0,
        )

    async def embed(self, entry: ModelEntry, request: EmbeddingRequest) -> Embedding:
        started = time.perf_counter()
        response: Any = await self._sent(
            self.client.embeddings.create(
                model=entry.model, input=request.texts, encoding_format="float"
            )
        )
        latency = (time.perf_counter() - started) * 1000
        vectors = [row.embedding for row in sorted(response.data, key=lambda row: row.index)]
        if len(vectors) != len(request.texts):
            raise ProviderError(
                f"{self.name} returned {len(vectors)} vectors for {len(request.texts)} texts",
                retryable=False,
            )
        usage = response.usage
        return Embedding(
            vectors=vectors,
            input_tokens=usage.prompt_tokens if usage else 0,
            model=str(response.model),
            latency_ms=latency,
        )

    async def _sent(self, call: Awaitable[Any]) -> Any:
        try:
            return await call
        except openai.APIStatusError as exc:
            raise ProviderError(
                f"{self.name} {exc.status_code}: {exc.message}",
                retryable=exc.status_code in (408, 409, 429) or exc.status_code >= 500,
            ) from exc
        except openai.APIConnectionError as exc:
            raise ProviderError(f"{self.name} connection error: {exc}", retryable=True) from exc
