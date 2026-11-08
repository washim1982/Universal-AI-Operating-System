# OpenAI clients and MCP

Programs that already speak OpenAI's chat API, and MCP clients such as AI assistants and IDEs, can
use AIOS Apps without AIOS code. Both use an AIOS credential (an API key or an identity provider's
token), and every request is a run of the App for that caller: the App's policies, budgets,
approvals and egress apply, and the run is traced like any other ([ADR-025](../adr/ADR-025-inbound-interoperability.md)).

| Door | Where | What a caller sees |
|---|---|---|
| OpenAI-compatible chat | `/openai/v1` | Each App with a chat entry point is a model |
| MCP server | `/mcp` (Streamable HTTP) | A tool `ask-<app>` for each such App, and `run-status` |

## OpenAI's clients

Set the client's base URL to `<server>/openai/v1` and its key to the AIOS credential, and use an
App's name as the model:

```python
import os

from openai import OpenAI

client = OpenAI(
    base_url=os.environ["AIOS_SERVER"] + "/openai/v1", api_key=os.environ["AIOS_API_KEY"]
)
print([model.id for model in client.models.list()])  # the Apps you can ask

reply = client.chat.completions.create(
    model="ai-researcher",
    messages=[
        {"role": "system", "content": "Answer briefly."},
        {"role": "user", "content": "What changed in the OpenTelemetry GenAI conventions?"},
    ],
)
print(reply.choices[0].message.content)
print(reply.model_extra["aios"])  # {"run_id": ..., "status": "completed", "trace": ...}
```

In JavaScript, the same with `new OpenAI({ baseURL, apiKey })`.

### What the App receives

The run's input is the last user message and the whole conversation:

```json
{"message": "What changed in the OpenTelemetry GenAI conventions?",
 "messages": [{"role": "system", "content": "Answer briefly."},
              {"role": "user", "content": "What changed in the OpenTelemetry GenAI conventions?"}]}
```

An App written for chat reads `${input.message}` and works as it is; one that wants the history
reads `${input.messages}`. Text parts of a message's content are joined; other parts are left out.

### What comes back

The answer is the run's output: text as it is; for an object, its `answer` (or `reply`, `text`,
`content`, `message`); otherwise its JSON in a code block. `usage` is the run's tokens. The `aios`
field holds the run's id, its status and the path of its trace.

A run that waits for a person, or takes longer than the server waits (`AIOS_ANSWER_WAIT`, two
minutes by default), comes back with a message that says so, and `aios.status` is `waiting` or
`running`. The run carries on; read it later at `/v1/apps/<app>/runs/<run-id>`.

With `stream: true`, the first chunk comes at once, a keep-alive comment every ten seconds, then the
answer in one chunk and the finish: an App answers whole. `stream_options: {"include_usage": true}`
adds the usage chunk.

### Headers and limits

| Header | Does |
|---|---|
| `X-AIOS-Session` | Go on with an earlier run's conversation, and its session memory |
| `X-AIOS-Data-Class` | Label the run (`public`, `internal`, `confidential`, `restricted`), which limits the models it can use |
| `traceparent` | Join your own trace |

`temperature` and other sampling settings are the App's to choose, and ignored. A request that
offers `tools` is refused (`400`, code `tools_not_supported`): an App calls its own tools, through
Tool OS.

| Status | When |
|---|---|
| `401` | No credential, or one AIOS doesn't know |
| `403` | The caller may not start runs of this App |
| `404`, code `model_not_found` | No App of that name in the caller's tenant with a chat entry point |
| `400` | Not a chat completion, no user message, `tools`, or a bad header |
| `502` | The run failed or was cancelled; the message is the run's error |

## MCP

Add an MCP server at `<server>/mcp`, over HTTP, with the header
`Authorization: Bearer <credential>`. For Claude Code:

```bash
claude mcp add --transport http aios "$AIOS_SERVER/mcp" --header "Authorization: Bearer $AIOS_API_KEY"
```

From Python, with the MCP SDK:

```python
import asyncio
import os

import httpx2
import mcp
from mcp.client.streamable_http import streamable_http_client


async def main() -> None:
    headers = {"Authorization": f"Bearer {os.environ['AIOS_API_KEY']}"}
    async with httpx2.AsyncClient(headers=headers, timeout=180) as http:
        transport = streamable_http_client(os.environ["AIOS_SERVER"] + "/mcp", http_client=http)
        async with mcp.Client(transport) as client:
            asked = {"message": "What changed in 2026?"}
            result = await client.call_tool("ask-ai-researcher", asked)
            print(result.content[0].text, result.structured_content["status"])


asyncio.run(main())
```

`tests/fixtures/apps/helper/clients/ask_mcp.py` is a fuller example.

| Tool | Arguments | Does |
|---|---|---|
| `ask-<app>` | `message`; `session` (optional) | Runs the App for the caller and returns its answer, or what the run waits for |
| `run-status` | `app`, `run_id`; `wait_seconds` (optional) | How a run stands: its answer, or what it waits for |

Each App's description is also a resource, `aios://apps/<app>`. A result's text is the answer; its
structured content is the run's state (`run_id`, `status`, `output`, `error`, `waiting`, `usage`,
`trace`). A failed run, a refusal and a bad argument are tool errors, so the assistant can say what
went wrong.

There is no tool to approve, deny or do a person's task: people decide those themselves, in the
console, a channel or the API. An assistant can only say that a run waits, and for whom.

The server is stateless, so any API replica answers. A local `aios dev` server, which has no
sign-in, answers MCP only at a loopback address (`127.0.0.1`, `localhost`, `[::1]`).

## Security

- A key acts for whoever holds it. Give an assistant or a service a key of its own, with only the
  roles it needs, and revoke it when it is done.
- An assistant passes on what it read, and that can carry instructions. An App that serves
  assistants, or anyone's words, can set `untrustedInput: true` on its chat entry point, so its
  runs are tainted and anything risky needs a person.
- The run's request event says which door it came through: `via: openai` or `via: mcp`.
