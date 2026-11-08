"""The LangGraph graph of Aios Template App.

AIOS calls ``build`` for each task, with a chat model whose calls go through the Model Kernel and
the AIOS tools the agent's manifest names in ``tools.allow``. Add a tool there, and the graph
gets a tools node.
"""

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

PROMPT = SystemMessage("Answer briefly and accurately.")


def build(model: BaseChatModel, tools: list[BaseTool]) -> Any:
    llm = model.bind_tools(tools) if tools else model

    async def think(state: MessagesState) -> dict[str, Any]:
        return {"messages": [await llm.ainvoke([PROMPT, *state["messages"]])]}

    graph = StateGraph(MessagesState)
    graph.add_node("think", think)
    graph.add_edge(START, "think")
    if tools:
        graph.add_node("tools", ToolNode(tools))
        graph.add_conditional_edges("think", tools_condition)
        graph.add_edge("tools", "think")
    else:
        graph.add_edge("think", END)
    return graph.compile()
