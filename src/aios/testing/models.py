"""A stand-in for every model, for an App's tests (WP 3.6).

    model = ScriptedModel(Reply.call("kb.article.find", q="refunds"), {"answer": "30 days"})
    async with LocalRuntime.from_app("my-app", model=model) as runtime:
        ...
    model.requests  # what each call asked

Replies come in order. A string is said as it is; a dict is said as JSON, as structured output
comes; a :class:`Reply` can ask for tool calls. A call with no reply left fails the run, and says
so, rather than inventing one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import JsonValue

from aios.core.catalog import ModelToolCall
from aios.core.ports import ModelRequest, model_tool_name
from aios.models import Completion, ModelEntry, Provider, ProviderError

PENDING = "call"


@dataclass(frozen=True)
class Reply:
    text: str = ""
    tool_calls: tuple[ModelToolCall, ...] = ()

    @classmethod
    def call(cls, tool: str, /, **arguments: JsonValue) -> Reply:
        """A reply that asks for one AIOS tool, by its own name (``kb.article.find``)."""
        call = ModelToolCall(id=PENDING, name=model_tool_name(tool), arguments=arguments)
        return cls(tool_calls=(call,))


class ScriptedModel(Provider):
    name = "scripted"

    def __init__(self, *replies: str | dict[str, Any] | Reply) -> None:
        self.replies = [_reply(reply) for reply in replies]
        self.requests: list[ModelRequest] = []

    async def complete(self, entry: ModelEntry, request: ModelRequest) -> Completion:
        self.requests.append(request)
        if not self.replies:
            raise ProviderError(
                f"the scripted model has no reply left for call {len(self.requests)}",
                retryable=False,
            )
        reply = self.replies.pop(0)
        calls = tuple(
            call.model_copy(update={"id": f"call_{len(self.requests)}_{index}"})
            if call.id == PENDING
            else call
            for index, call in enumerate(reply.tool_calls)
        )
        return Completion(
            text=reply.text,
            input_tokens=sum(len(m.content) for m in request.messages) // 4 + 1,
            output_tokens=len(reply.text) // 4 + 1,
            model=entry.model,
            tool_calls=calls,
        )


def _reply(reply: str | dict[str, Any] | Reply) -> Reply:
    if isinstance(reply, Reply):
        return reply
    if isinstance(reply, dict):
        return Reply(text=json.dumps(reply))
    return Reply(text=reply)
