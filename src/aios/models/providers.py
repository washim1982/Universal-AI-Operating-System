"""Provider adapters over the official SDKs (ADR-008, FR-MOD-02).

Each adapter turns a provider-neutral :class:`ModelRequest` into one provider call and returns
a :class:`Completion`: the text, the token counts and the model that actually served it.
Routing, fallback, accounting and recording stay in the kernel.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, cast

import anthropic
import httpx2
import openai

from aios.core.errors import ModelError
from aios.core.ports import ModelRequest
from aios.models.registry import ModelEntry

ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    stop_reason: str | None = None
    latency_ms: float = 0.0


class ProviderError(ModelError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class Provider(ABC):
    name: str

    @abstractmethod
    async def complete(self, entry: ModelEntry, request: ModelRequest) -> Completion: ...


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
        prompt_tokens = (
            usage.input_tokens
            + (getattr(usage, "cache_creation_input_tokens", None) or 0)
            + (getattr(usage, "cache_read_input_tokens", None) or 0)
        )
        return Completion(
            text=text,
            input_tokens=prompt_tokens,
            output_tokens=usage.output_tokens,
            model=str(message.model),
            stop_reason=message.stop_reason,
            latency_ms=latency,
        )


class OpenAIProvider(Provider):
    """OpenAI, or any OpenAI-compatible server (vLLM, Ollama, ...) with ``base_url``."""

    def __init__(
        self,
        *,
        name: str = "openai",
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 2,
        timeout: float = 600.0,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self.name = name
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
        started = time.perf_counter()
        try:
            response: Any = cast(Any, await self.client.chat.completions.create(**params))
        except openai.APIStatusError as exc:
            raise ProviderError(
                f"{self.name} {exc.status_code}: {exc.message}",
                retryable=exc.status_code in (408, 409, 429) or exc.status_code >= 500,
            ) from exc
        except openai.APIConnectionError as exc:
            raise ProviderError(f"{self.name} connection error: {exc}", retryable=True) from exc
        latency = (time.perf_counter() - started) * 1000
        choice = response.choices[0]
        usage = response.usage
        return Completion(
            text=cast(str, choice.message.content or ""),
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            model=str(response.model),
            stop_reason=choice.finish_reason,
            latency_ms=latency,
        )
