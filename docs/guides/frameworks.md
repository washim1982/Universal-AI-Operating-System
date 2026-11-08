# Other frameworks

Agents built with **LangGraph** or the **OpenAI Agents SDK** run in AIOS as they are, as AIOS
agents: they get tasks for their capabilities, and their runs are traced, budgeted, replayable and
governed. Install the framework with AIOS:

```bash
pip install "aios[langgraph]"        # or "aios[openai-agents]"
```

Two rules make it governed:

- **Every model call goes through AIOS.** The adapter gives the framework a model that is the Model
  Kernel: the model policy picks the model (any provider, not only the framework's own), and
  budgets, data classes and recordings apply.
- **AIOS tools are called through Tool OS.** The tools the adapter gives the framework are AIOS
  tools, so policy, approvals and egress rules apply. A tool written in the agent's own code is
  the agent's own code, as in any Python agent.

The tools offered are those named exactly in the agent's `tools.allow` (a pattern such as
`search.>` allows a family, but names no tool to offer).

## LangGraph

```yaml
apiVersion: aios/v1alpha1
kind: Agent
metadata: {name: graph-agent, version: 0.1.0}
spec:
  runtime: {type: "adapter:langgraph", entrypoint: "my_shop.graph:build"}
  capabilities: [{name: answering}]
  model: {task: reasoning}
  tools: {allow: ["kb.article.find"]}
```

The entrypoint is a function that builds the graph from a chat model and tools, which AIOS calls
for each task:

```python
from langchain_core.messages import SystemMessage
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition


def build(model, tools):
    llm = model.bind_tools(tools)

    async def think(state: MessagesState):
        prompt = SystemMessage("Answer from the help articles.")
        return {"messages": [await llm.ainvoke([prompt, *state["messages"]])]}

    graph = StateGraph(MessagesState)
    graph.add_node("think", think)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "think")
    graph.add_conditional_edges("think", tools_condition)
    graph.add_edge("tools", "think")
    return graph.compile()
```

Or name a compiled graph, and use `ChatAIOS()` (and `await aios_tools(...)`) from
`aios.adapters.langgraph` inside its nodes. A task's `{"message": ...}` starts the graph's
`messages`; the output is the final state, with `answer`, the last thing the model said.

## OpenAI Agents SDK

```yaml
  runtime: {type: "adapter:openai-agents", entrypoint: "my_shop.agents:build"}
```

```python
from agents import Agent


def build(tools):
    return Agent(name="Helper", instructions="Answer from the help articles.", tools=tools)
```

Or name an `Agent` itself. Every agent of the run, handoffs included, gets the AIOS model, whatever
model it names. The SDK's own tracing is off: AIOS traces the run, and nothing goes to OpenAI's
trace service. Tools hosted by OpenAI (web search, file search, computer use) are refused, because
they would run outside AIOS; use function tools. The output is `{"answer": ...}`, or the agent's
structured output.

## Start from a template

```bash
aios init my-shop --template langgraph          # or openai-agents
aios init agent another-agent --app my-shop --template langgraph
```
