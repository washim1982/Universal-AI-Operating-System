"""LangGraph graphs as AIOS agents (FR-INT-04; WP 3.6)."""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from aios.adapters import handler_for
from aios.adapters.langgraph import ChatAIOS, _turns  # pyright: ignore[reportPrivateUsage]
from aios.core.manifests import AgentManifest, load_manifests

from .conftest import DESK


def build(model: Any, tools: list[Any]) -> Any:
    """A ReAct loop: the model thinks, asks for tools, and thinks again with what they said."""
    llm = model.bind_tools(tools)

    async def think(state: MessagesState) -> dict[str, Any]:
        prompt = SystemMessage("Answer from the help articles.")
        return {"messages": [await llm.ainvoke([prompt, *state["messages"]])]}

    graph = StateGraph(MessagesState)
    graph.add_node("think", think)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "think")
    graph.add_conditional_edges("think", tools_condition)
    graph.add_edge("tools", "think")
    return graph.compile()


async def test_a_react_agent_answers_through_aios(desk: Any, module: Any) -> None:
    where = module("lg_react", {"build": build})
    found = await desk("langgraph", f"{where}:build")
    run = await found.runtime.chat("helpdesk", "How long do refunds take?")
    result = await run.wait(timeout=15)
    assert result.status == "completed", result.error
    assert result.output["answer"] == "Refunds take 30 days."
    assert [m["role"] for m in result.output["messages"]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert found.asked == ["refunds"]  # the tool ran once, through Tool OS
    first, second = found.model.requests
    assert first.system == "Answer from the help articles."
    assert [t.name for t in first.tools] == ["kb__article__find"]
    assert first.task == "planning"
    assert [m.role for m in second.messages] == ["user", "assistant", "tool"]
    types = [e.type for e in result.events]
    assert types.count("model.call.completed") == 2  # each one routed, priced and recorded
    assert types.count("tool.call.completed") == 1


async def _answer(state: MessagesState) -> dict[str, Any]:
    reply = await ChatAIOS(task="summarizing").ainvoke(state["messages"])
    return {"messages": [reply]}


def _graph() -> Any:
    graph = StateGraph(MessagesState)
    graph.add_node("answer", _answer)
    graph.add_edge(START, "answer")
    graph.add_edge("answer", END)
    return graph.compile()


async def test_a_compiled_graph_is_hosted_as_it_is(desk: Any, module: Any) -> None:
    where = module("lg_graph", {"graph": _graph()})
    found = await desk("langgraph", f"{where}:graph")
    result = await (await found.runtime.chat("helpdesk", "Refund window?")).wait(timeout=15)
    assert result.status == "completed", result.error
    assert result.output["answer"] == "Refunds take 30 days."
    [asked] = found.model.requests
    assert asked.task == "summarizing"  # its own routing hint
    assert asked.tools == []
    assert found.asked == []


def test_messages_become_turns() -> None:
    system, turns = _turns(
        [
            SystemMessage("Be brief."),
            HumanMessage("Refund window?"),
            AIMessage(
                "", tool_calls=[{"name": "kb__article__find", "args": {"q": "r"}, "id": "c1"}]
            ),
            ToolMessage('{"days": 30}', tool_call_id="c1"),
        ]
    )
    assert system == "Be brief."
    assert [t.role for t in turns] == ["user", "assistant", "tool"]
    assert turns[1].tool_calls[0].arguments == {"q": "r"}
    assert turns[2].tool_call_id == "c1"


def test_a_missing_framework_says_how_to_get_it(monkeypatch: pytest.MonkeyPatch) -> None:
    [manifest] = [
        m
        for m in load_manifests(DESK.format(framework="crewai", entrypoint="x:y"))
        if isinstance(m, AgentManifest)
    ]
    with pytest.raises(ValueError, match="no adapter for 'crewai'"):
        handler_for(manifest)


def test_only_tools_named_exactly_are_offered() -> None:
    from aios.adapters import offered_tools  # noqa: PLC0415

    text = DESK.format(framework="langgraph", entrypoint="x:y").replace(
        'allow: ["kb.article.find"]', 'allow: ["kb.>", "kb.article.find", "search.*.query"]'
    )
    [manifest] = [m for m in load_manifests(text) if isinstance(m, AgentManifest)]
    assert offered_tools(manifest) == ["kb.article.find"]
