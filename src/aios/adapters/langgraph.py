"""LangGraph graphs as AIOS agents (FR-INT-04).

The entrypoint is a compiled graph, or a function that builds one from a chat model and tools:

    def build(model, tools):
        return create_react_agent(model, tools)

``model`` is :class:`ChatAIOS`, whose every call goes through the Model Kernel. ``tools`` are the
AIOS tools the agent's manifest names exactly in ``tools.allow``, each called through Tool OS. A
compiled graph can use them too: ``ChatAIOS()``, and ``await aios_tools(...)`` inside a node.

A task's input becomes the graph's state: ``{"message": "..."}`` starts ``messages`` with it. The
output is the final state, with its messages as plain role and content, and ``answer``, the last
one the model said.
"""
# pyright: reportIncompatibleMethodOverride=false, reportIncompatibleVariableOverride=false

from __future__ import annotations

import inspect
import json
from collections.abc import Sequence
from typing import Any, cast

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from aios.adapters import Handler, Running, current, offered_tools
from aios.core.catalog import ModelToolCall
from aios.core.manifests import AgentManifest
from aios.core.ports import Message, ModelTool
from aios.kernel import Context

CHOICES = {"auto": "auto", "any": "required", "required": "required", "none": "none"}


class ChatAIOS(BaseChatModel):
    """A LangChain chat model that is the AIOS Model Kernel: the policy picks the model."""

    task: str | None = None
    """The routing hint for the model policy; the agent's own ``model.task`` if not set."""
    max_tokens: int = 16_000
    temperature: float | None = None

    @property
    def _llm_type(self) -> str:
        return "aios"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Any | BaseTool],
        *,
        tool_choice: str | bool | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, AIMessage]:
        offered = [convert_to_openai_tool(tool) for tool in tools]
        return self.bind(tools=offered, tool_choice=_choice(tool_choice), **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("AIOS agents run asynchronously: use ainvoke")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        system, turns = _turns(messages)
        specs = cast(list[dict[str, dict[str, Any]]], kwargs.get("tools") or [])
        offered = [
            ModelTool(
                name=spec["function"]["name"],
                description=spec["function"].get("description", ""),
                parameters=spec["function"].get("parameters") or {"type": "object"},
            )
            for spec in specs
        ]
        reply = await current().model.complete(
            turns,
            tools=offered,
            tool_choice=kwargs.get("tool_choice") if offered else None,
            task=self.task,
            system=system,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        message = AIMessage(
            content=reply.text,
            tool_calls=[
                {"name": call.name, "args": call.arguments, "id": call.id, "type": "tool_call"}
                for call in reply.tool_calls
            ],
            usage_metadata={
                "input_tokens": reply.usage.input_tokens,
                "output_tokens": reply.usage.output_tokens,
                "total_tokens": reply.usage.input_tokens + reply.usage.output_tokens,
            },
            response_metadata={"provider": reply.provider, "model_name": reply.model},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def _choice(choice: str | bool | None) -> str | None:
    if choice is None or choice is False:
        return None
    if choice is True:
        return "required"
    return CHOICES.get(choice, "required")  # a tool's own name: the model must call one


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = cast(list[Any], content)
    return "".join(
        part if isinstance(part, str) else str(cast(dict[str, Any], part).get("text", ""))
        for part in parts
    )


def _turns(messages: list[BaseMessage]) -> tuple[str | None, list[Message]]:
    system: list[str] = []
    turns: list[Message] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            system.append(_text(message.content))
        elif isinstance(message, AIMessage):
            calls = [
                ModelToolCall(id=str(call["id"]), name=call["name"], arguments=call["args"])
                for call in message.tool_calls
            ]
            turns.append(
                Message(role="assistant", content=_text(message.content), tool_calls=calls)
            )
        elif isinstance(message, ToolMessage):
            turns.append(
                Message(
                    role="tool", tool_call_id=message.tool_call_id, content=_text(message.content)
                )
            )
        elif isinstance(message, HumanMessage):
            turns.append(Message(role="user", content=_text(message.content)))
        else:
            raise ValueError(f"AIOS can't send a {type(message).__name__} to a model")
    return ("\n\n".join(system) or None), turns


async def aios_tools(*names: str) -> list[BaseTool]:
    """AIOS tools as LangChain tools, called through Tool OS. Use them inside a running task."""
    ctx = current()
    described = await ctx.tools.describe(*names)
    return [_tool(name, model) for name, model in zip(names, described, strict=True)]


def _tool(name: str, offered: ModelTool) -> BaseTool:
    async def run(**arguments: Any) -> str:
        found = await current().tools.invoke(name, **arguments)
        return json.dumps(found, ensure_ascii=False)

    return StructuredTool(
        name=offered.name,
        description=offered.description or name,
        args_schema=offered.parameters,
        coroutine=run,
    )


def handler(target: Any, manifest: AgentManifest) -> Handler:
    names = offered_tools(manifest)
    limit = manifest.spec.limits.max_steps

    async def run(ctx: Context, task: Any) -> Any:
        with Running(ctx):
            graph = target
            if not hasattr(target, "ainvoke"):
                built = target(ChatAIOS(task=manifest.spec.model.task), await aios_tools(*names))
                graph = await built if inspect.isawaitable(built) else built
            config: dict[str, Any] = {"recursion_limit": 2 * limit + 1} if limit else {}
            final = await graph.ainvoke(_state(task), config=config)
        return _output(final)

    run.__name__ = manifest.metadata.name.replace("-", "_")
    return run


def _state(task: Any) -> Any:
    if not isinstance(task, dict):
        return task
    given = cast(dict[str, Any], task)
    if "message" not in given or "messages" in given:
        return given
    rest = {key: value for key, value in given.items() if key != "message"}
    return {"messages": [{"role": "user", "content": str(given["message"])}], **rest}


def _output(state: Any) -> Any:
    if not isinstance(state, dict):
        return state
    found = cast(dict[str, Any], state)
    if "messages" not in found:
        return found
    messages = [_plain(m) for m in found["messages"]]
    said = [m["content"] for m in messages if m["role"] == "assistant" and m["content"]]
    rest = {key: value for key, value in found.items() if key != "messages"}
    return {"answer": said[-1] if said else "", "messages": messages, **rest}


def _plain(message: Any) -> dict[str, Any]:
    if isinstance(message, BaseMessage):
        role = {"human": "user", "ai": "assistant"}.get(message.type, message.type)
        return {"role": role, "content": _text(message.content)}
    return cast(dict[str, Any], message)
