"""OpenAI Agents SDK agents as AIOS agents (FR-INT-04).

The entrypoint is an ``agents.Agent``, or a function that builds one from tools:

    def build(tools):
        return Agent(name="Helper", instructions="...", tools=tools)

Every agent of the run, handoffs included, is given :class:`AIOSModel`, so each model call goes
through the Model Kernel, whatever model the agent names. ``tools`` are the AIOS tools the agent's
manifest names exactly in ``tools.allow``, called through Tool OS; ``await aios_tools(...)`` gives
them to an agent built some other way. The SDK's own tracing is off: the run is traced by AIOS, and
nothing is sent to OpenAI's trace service. Tools hosted by OpenAI (web search, file search,
computer use) aren't available: they would run outside AIOS.

A task's ``message`` (or the whole input, as JSON) is the user's turn. The output is the final
output: ``{"answer": text}``, or the agent's structured output as it is.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator
from typing import Any, cast

from agents import (
    Agent,
    FunctionTool,
    Handoff,
    Model,
    ModelProvider,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    RunConfig,
    RunContextWrapper,
    Runner,
    Tool,
    Usage,
    UserError,
)
from agents.agent_output import AgentOutputSchemaBase
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from pydantic import BaseModel

from aios.adapters import Handler, Running, current, offered_tools
from aios.core.catalog import ModelToolCall
from aios.core.manifests import AgentManifest
from aios.core.ports import Message, ModelTool
from aios.kernel import Context

DEFAULT_TURNS = 10


class AIOSModel(Model):
    """The Model Kernel, as the SDK's ``Model``: the model policy picks the model."""

    def __init__(self, task: str | None = None) -> None:
        self.task = task

    async def get_response(  # noqa: PLR0917 - the SDK's signature
        self,
        system_instructions: str | None,
        input: str | list[Any],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff[Any, Any]],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any = None,
    ) -> ModelResponse:
        offered = [_offered(tool) for tool in tools] + [
            ModelTool(
                name=handoff.tool_name,
                description=handoff.tool_description,
                parameters=handoff.input_json_schema or {"type": "object"},
            )
            for handoff in handoffs
        ]
        system, turns = _turns(input)
        reply = await current().model.complete(
            turns,
            tools=offered,
            tool_choice=_choice(model_settings.tool_choice) if offered else None,
            task=self.task,
            system="\n\n".join(p for p in (system_instructions, system) if p) or None,
            output_schema=output_schema.json_schema()
            if output_schema is not None and not output_schema.is_plain_text()
            else None,
            max_tokens=model_settings.max_tokens or 16_000,
            temperature=model_settings.temperature,
        )
        output: list[Any] = []
        if reply.text:
            output.append(
                ResponseOutputMessage(
                    id="msg_aios",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(type="output_text", text=reply.text, annotations=[])
                    ],
                )
            )
        output += [
            ResponseFunctionToolCall(
                type="function_call",
                call_id=call.id,
                name=call.name,
                arguments=json.dumps(call.arguments),
                status="completed",
            )
            for call in reply.tool_calls
        ]
        usage = Usage(
            requests=1,
            input_tokens=reply.usage.input_tokens,
            output_tokens=reply.usage.output_tokens,
            total_tokens=reply.usage.input_tokens + reply.usage.output_tokens,
        )
        return ModelResponse(output=output, usage=usage, response_id=None)

    def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        raise UserError("AIOS runs agents to completion: use Runner.run, not run_streamed")


class AIOSModelProvider(ModelProvider):
    def __init__(self, task: str | None = None) -> None:
        self.task = task

    def get_model(self, model_name: str | None) -> Model:
        return AIOSModel(self.task)


def _offered(tool: Tool) -> ModelTool:
    if not isinstance(tool, FunctionTool):
        raise UserError(
            f"{type(tool).__name__} runs outside AIOS; give the agent function tools instead"
        )
    return ModelTool(
        name=tool.name, description=tool.description, parameters=tool.params_json_schema
    )


def _choice(choice: Any) -> Any:
    if choice in (None, "auto", "required", "none"):
        return choice
    return "required"  # a tool's own name: the model must call one


def _item(item: Any) -> dict[str, Any]:
    if isinstance(item, BaseModel):
        return item.model_dump(exclude_unset=True)
    return cast(dict[str, Any], item)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        str(part.get("text", "")) for part in (_item(p) for p in cast(list[Any], content or []))
    )


def _turns(input: str | list[Any]) -> tuple[str | None, list[Message]]:
    """The SDK's input items as AIOS turns. A run of function calls joins the assistant turn
    before it, since providers want an assistant's calls in one turn."""
    if isinstance(input, str):
        return None, [Message(role="user", content=input)]
    system: list[str] = []
    turns: list[Message] = []
    for raw in input:
        item = _item(raw)
        kind = item.get("type", "message")
        if kind == "function_call":
            call = ModelToolCall(
                id=str(item["call_id"]),
                name=str(item["name"]),
                arguments=json.loads(item.get("arguments") or "{}"),
            )
            last = turns[-1] if turns else None
            if last is not None and last.role == "assistant":
                turns[-1] = last.model_copy(update={"tool_calls": [*last.tool_calls, call]})
            else:
                turns.append(Message(role="assistant", tool_calls=[call]))
        elif kind == "function_call_output":
            output = item.get("output")
            content = output if isinstance(output, str) else _text(output)
            turns.append(Message(role="tool", tool_call_id=str(item["call_id"]), content=content))
        elif kind == "message":
            role = item.get("role")
            text = _text(item.get("content"))
            if role in ("system", "developer"):
                system.append(text)
            elif role == "assistant":
                turns.append(Message(role="assistant", content=text))
            else:
                turns.append(Message(role="user", content=text))
        # reasoning and other items say nothing a model needs to be told again
    return ("\n\n".join(system) or None), turns


async def aios_tools(*names: str) -> list[FunctionTool]:
    """AIOS tools as the SDK's function tools, described and called through Tool OS. Use them
    inside a running task."""
    described = await current().tools.describe(*names)
    return [_tool(name, model) for name, model in zip(names, described, strict=True)]


def _tool(name: str, offered: ModelTool) -> FunctionTool:
    async def run(_: RunContextWrapper[Any], arguments: str) -> str:
        found = await current().tools.invoke(name, **json.loads(arguments or "{}"))
        return json.dumps(found, ensure_ascii=False)

    return FunctionTool(
        name=offered.name,
        description=offered.description or name,
        params_json_schema=offered.parameters,
        on_invoke_tool=run,
        strict_json_schema=False,
    )


def handler(target: Any, manifest: AgentManifest) -> Handler:
    names = offered_tools(manifest)
    turns = manifest.spec.limits.max_steps or DEFAULT_TURNS

    async def run(ctx: Context, task: Any) -> Any:
        with Running(ctx):
            agent = target
            if not isinstance(target, Agent):
                built = target(await aios_tools(*names))
                agent = await built if inspect.isawaitable(built) else built
            config = RunConfig(
                model_provider=AIOSModelProvider(manifest.spec.model.task),
                tracing_disabled=True,
                workflow_name=manifest.metadata.name,
            )
            result = await Runner.run(
                cast(Agent[Any], agent), _input(task), run_config=config, max_turns=turns
            )
        return _output(result.final_output)

    run.__name__ = manifest.metadata.name.replace("-", "_")
    return run


def _input(task: Any) -> str:
    if isinstance(task, str):
        return task
    if isinstance(task, dict):
        message = cast(dict[str, Any], task).get("message")
        if isinstance(message, str):
            return message
    return json.dumps(task)


def _output(final: Any) -> Any:
    if isinstance(final, BaseModel):
        return final.model_dump(mode="json")
    if isinstance(final, str):
        return {"answer": final}
    return final


__all__ = ["AIOSModel", "AIOSModelProvider", "aios_tools", "handler"]
