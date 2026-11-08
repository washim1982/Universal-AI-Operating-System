# Build agents

An agent is an async Python function with a capability. AIOS gives it the task's input and a
`ctx`, through which it does everything else:

```python
from pydantic import BaseModel

from aios import Context, agent


class Question(BaseModel):
    message: str


class Answer(BaseModel):
    answer: str


@agent("assistant-agent", capability="answering")
async def answer(ctx: Context, task: Question) -> Answer:
    return await ctx.model.generate(
        task="reasoning",
        system="Answer briefly and accurately.",
        messages=[ctx.user(task.message)],
        output=Answer,
    )
```

The input is validated against the parameter's type, and the result against the return type.
Name the function in the agent's manifest: `runtime: {type: python, entrypoint: "my_shop.agents:answer"}`.

## Models

`ctx.model.generate(...)` asks the model the policy routes `task` to, and with `output=` returns
validated structured output. Every call is priced, recorded and charged to the run's budget.

`ctx.model.complete(...)` returns the whole reply, including any **tool calls** the model asks
for. Asking is all a model does: the agent makes the calls, through Tool OS.

```python
import json

from aios import Context, Message, agent


@agent("helper", capability="help.answer")
async def helper(ctx: Context, task: dict) -> dict:
    tools = await ctx.tools.describe("kb.article.find", "orders.status.find")
    messages = [Message(role="user", content=task["message"])]
    for _ in range(5):
        reply = await ctx.model.complete(messages, tools=tools, system="Use the tools.")
        if not reply.tool_calls:
            return {"answer": reply.text}
        messages.append(Message(role="assistant", content=reply.text, tool_calls=reply.tool_calls))
        for call in reply.tool_calls:
            found = await ctx.tools.call(call)  # through Tool OS: policy, approvals, egress
            messages.append(Message(role="tool", tool_call_id=call.id, content=json.dumps(found)))
    return {"answer": "I couldn't finish."}
```

A model sees `kb.article.find` as `kb__article__find`: providers allow no dots in a tool's name.
A tool the agent isn't allowed stays unused however much the model asks for it.

## Tools

```python
receipt = await ctx.tools.invoke(
    "orders.refund.create",
    order="1001",
    amountUSD=120,
    rationale="The customer says it arrived broken.",
)
```

The agent's manifest lists what it may use (`tools: {allow: [...]}`), and the App's policies say
what is allowed and when a person must agree. A call that needs a person stops the task; once
someone answers, the task runs again from the top, its earlier calls answered from the record, and
this line returns, or raises `ApprovalDeniedError`:

```python
from aios.core.errors import ApprovalDeniedError

try:
    receipt = await ctx.tools.invoke("orders.refund.create", order="1001", amountUSD=500)
except ApprovalDeniedError:
    return {"reply": "A person said no, so I can't refund this one."}
```

`rationale` is shown to the approver, labelled as the agent's own words.

Write a tool of your own with `@tool`, and say what kind of side effect it has. A `previewer` gives
approvers a dry run:

```python
from aios.core.policy import SideEffect
from aios.tools import NativeProvider, tool


@tool("orders.refund.create", side_effect=SideEffect.FINANCIAL, description="Refund an order.")
async def refund(order: str, amountUSD: float) -> dict: ...


@refund.previewer
async def what_it_would_do(order: str, amountUSD: float) -> str:
    return f"would refund ${amountUSD:.2f} for order {order}"


def providers():
    return [NativeProvider("orders", [refund])]
```

Start AIOS with `AIOS_TOOL_PROVIDERS=my_shop.tools:providers`. `aios init tool <name> --app .`
writes the start of one.

## Memory

```python
pack = await ctx.memory.recall(task.message, scopes=["app"])  # cited, within a token budget
await ctx.memory.put("The customer prefers email.", scope="user")
```

`str(pack)` is the text, marked as data so a model can tell it from instructions; `pack.citations`
says where each part came from. An agent reads and writes only the scopes its manifest lists
(`memory: {read: [app], write: [session]}`), and policy decides each one.

## Other things on `ctx`

- `ctx.emit("refund.proposed", {...})` publishes an event of the App's own.
- `ctx.user(text)`, `ctx.assistant(text)` make messages; `ctx.quote(text, source=...)` marks
  outside content as data.
- `ctx.run_id`, `ctx.step_key(name)` (a stable key for idempotency), `ctx.logger`.

## Test it

`LocalRuntime` runs an App in memory. `ScriptedModel` stands in for every model, so a test needs
no key and costs nothing:

```python
from pathlib import Path

from aios.testing.models import Reply, ScriptedModel
from aios.testing.runtime import LocalRuntime

APP = Path(__file__).parents[1]


async def test_it_looks_the_answer_up() -> None:
    model = ScriptedModel(Reply.call("kb.article.find", q="refunds"), "Refunds take 30 days.")
    async with LocalRuntime.from_app(APP, model=model, tool_providers=providers()) as runtime:
        result = await (await runtime.chat("my-shop", "How long do refunds take?")).wait()
    assert result.status == "completed"
    assert result.output == {"answer": "Refunds take 30 days."}
    first, second = model.requests  # what each call asked
    assert second.messages[-1].role == "tool"
```

A dict reply is said as JSON, for structured output. `runtime.approve(id)`, `runtime.deny(id)`
and `runtime.ingest_knowledge(app, folder)` help with the rest. For runs made with a real model,
record them once (`--cassettes` with `AIOS_CASSETTE_MODE=record`) and replay them in CI.
