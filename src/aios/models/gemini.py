"""Google Gemini through the official ``google-genai`` SDK (ADR-008, FR-MOD-02).

In a module of its own because the SDK takes most of a second to import, and the kernel only needs
it once a policy routes to a ``google/...`` model. Gemini caches long prompts by itself, so there is
nothing to ask for; the adapter reports what it found (FR-MOD-09).
"""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import time
from collections.abc import Awaitable
from typing import Any, cast

import httpx2
from google import genai
from google.genai import errors, types

from aios.core.catalog import ModelToolCall
from aios.core.ports import EmbeddingRequest, Message, ModelRequest
from aios.models.providers import Completion, Embedding, Provider, ProviderError
from aios.models.registry import ModelEntry

ROLES = {"user": "user", "assistant": "model"}
REFUSED = ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII")


class GeminiProvider(Provider):
    name = "google"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 600.0,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        options = types.HttpOptions(
            base_url=base_url, timeout=int(timeout * 1000), httpx_async_client=http_client
        )
        try:
            self.client = genai.Client(api_key=api_key, http_options=options)
        except ValueError as exc:  # no key: the next candidate can still answer
            raise ProviderError(f"google: {exc}", retryable=False) from exc

    async def complete(self, entry: ModelEntry, request: ModelRequest) -> Completion:
        config = types.GenerateContentConfig(
            system_instruction=request.system,
            max_output_tokens=request.max_tokens,
            temperature=request.temperature,
        )
        if request.output_schema is not None:
            config.response_mime_type = "application/json"
            config.response_json_schema = request.output_schema
        if request.tools:
            config.tools = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t.name,
                            description=t.description,
                            parameters_json_schema=t.parameters,
                        )
                        for t in request.tools
                    ]
                )
            ]
            if request.tool_choice is not None:
                mode = {"auto": "AUTO", "required": "ANY", "none": "NONE"}[request.tool_choice]
                config.tool_config = types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode=cast(Any, mode))
                )
        contents = _contents(request.messages)
        started = time.perf_counter()
        response = await self._sent(
            self.client.aio.models.generate_content(
                model=entry.model, contents=cast(Any, contents), config=config
            )
        )
        latency = (time.perf_counter() - started) * 1000
        return _completion(entry, response, latency)

    async def embed(self, entry: ModelEntry, request: EmbeddingRequest) -> Embedding:
        started = time.perf_counter()
        response = await self._sent(
            self.client.aio.models.embed_content(model=entry.model, contents=list(request.texts))
        )
        latency = (time.perf_counter() - started) * 1000
        vectors = [cast(list[float], row.values) for row in response.embeddings or []]
        if len(vectors) != len(request.texts):
            raise ProviderError(
                f"google returned {len(vectors)} vectors for {len(request.texts)} texts",
                retryable=False,
            )
        # The Gemini API doesn't count an embedding's tokens; this is the estimate rank.py uses.
        tokens = sum(len(text) for text in request.texts) // 4
        return Embedding(
            vectors=vectors, input_tokens=tokens, model=entry.model, latency_ms=latency
        )

    async def _sent(self, call: Awaitable[Any]) -> Any:
        try:
            return await call
        except errors.APIError as exc:
            status = exc.code or 0
            raise ProviderError(
                f"google {status}: {exc.message}",
                retryable=status in (408, 409, 429) or status >= 500,
            ) from exc
        except (httpx2.TransportError, OSError) as exc:
            raise ProviderError(f"google connection error: {exc}", retryable=True) from exc


def _completion(entry: ModelEntry, response: Any, latency: float) -> Completion:
    feedback = response.prompt_feedback
    if feedback is not None and feedback.block_reason is not None:
        raise ProviderError(f"{entry.model} declined the request", retryable=False)
    candidates = response.candidates or []
    if not candidates:
        raise ProviderError(f"{entry.model} returned no candidates", retryable=True)
    finish = candidates[0].finish_reason
    reason = None if finish is None else str(getattr(finish, "value", finish))
    if reason in REFUSED:
        raise ProviderError(f"{entry.model} declined the request", retryable=False)
    usage = response.usage_metadata
    parts = (candidates[0].content.parts if candidates[0].content else None) or []
    text = "".join(part.text for part in parts if part.text and not part.thought)
    calls = tuple(
        ModelToolCall(
            id=part.function_call.id or f"call_{index}",
            name=part.function_call.name or "",
            arguments=dict(part.function_call.args or {}),
        )
        for index, part in enumerate(parts)
        if part.function_call is not None
    )
    return Completion(
        text=text,
        input_tokens=(usage.prompt_token_count or 0) if usage else 0,
        output_tokens=(usage.candidates_token_count or 0) if usage else 0,
        model=str(response.model_version or entry.model),
        stop_reason=reason,
        latency_ms=latency,
        cache_read_tokens=(usage.cached_content_token_count or 0) if usage else 0,
        tool_calls=calls,
    )


def _contents(messages: list[Message]) -> list[types.Content]:
    """Turns as Gemini wants them: a tool call is a ``function_call`` part of the model's turn,
    and its answer a ``function_response`` part, which names the function it answers."""
    names = {call.id: call.name for m in messages for call in m.tool_calls}
    contents: list[types.Content] = []
    for message in messages:
        if message.role == "tool":
            answer = types.Part(
                function_response=types.FunctionResponse(
                    id=message.tool_call_id,
                    name=names.get(message.tool_call_id or "", "tool"),
                    response={"result": message.content},
                )
            )
            last = contents[-1] if contents else None
            if (
                last is not None
                and last.role == "user"
                and last.parts
                and last.parts[0].function_response
            ):
                last.parts.append(answer)
            else:
                contents.append(types.Content(role="user", parts=[answer]))
            continue
        parts = [types.Part(text=message.content)] if message.content else []
        parts += [
            types.Part(
                function_call=types.FunctionCall(id=call.id, name=call.name, args=call.arguments)
            )
            for call in message.tool_calls
        ]
        contents.append(types.Content(role=ROLES[message.role], parts=parts))
    return contents
