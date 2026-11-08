# Quickstart

Build an App, test it, run it, and install it into a tenant. About fifteen minutes; no model key is
needed until you talk to a real model.

## 1. Install AIOS

From a checkout of AIOS:

```bash
uv sync
```

Then use `uv run aios ...`, or activate the environment (`.venv\Scripts\activate` on Windows,
`source .venv/bin/activate` elsewhere) and type `aios ...`. Check it:

```bash
aios --version
```

## 2. Make an App from a template

```bash
aios init my-shop --template chat
```

There are six templates:

| Template | What you get |
|---|---|
| `chat` | One agent that answers a message with a model |
| `rag` | An agent that answers from the App's own documents, and says which |
| `approval` | An agent with its own tool; a big action waits for a person, who sees a dry run |
| `people` | A step that people do, as an agent would, then an agent's reply |
| `langgraph` | An agent that is a LangGraph graph (needs `aios[langgraph]`) |
| `openai-agents` | An agent built with the OpenAI Agents SDK (needs `aios[openai-agents]`) |

Each makes a folder like this:

```text
my-shop/
  manifests/app.yaml      the App, its agents and its workflow
  src/my_shop/agents.py   the agents' code
  tests/test_app.py       a test that runs the App
  README.md               how to run, test and install it
  pyproject.toml          settings for its tests
```

Open `manifests/app.yaml`. An **App** lists its entry points (here, chat), its agents and its
workflows. An **Agent** names its code (`runtime.entrypoint`) and the **capabilities** it
provides. A **Workflow** is the steps a run takes; a `task` step asks for a capability, and an
agent that provides it does the work.

## 3. Test it

```bash
cd my-shop
pytest
```

The test runs the whole App in memory with a stand-in model, `ScriptedModel`, that says what the
test tells it to. Nothing is called and nothing is spent. Change the reply in `tests/test_app.py`
and watch the test follow.

## 4. Check it

```bash
aios validate .
```

`validate` loads the App as AIOS would: its manifests, its policies and its code. Then it looks
for what would otherwise only show up when it runs: a step that asks for something nobody
provides, an agent no step uses, a tool no policy allows.

## 5. Run it

Set a model key first, for example `ANTHROPIC_API_KEY`, or point AIOS at a model on your own server
with `AIOS_PRIVATE_MODEL` and `AIOS_PRIVATE_MODEL_URL` (see the [user guide](user-guide.md)). Then:

```bash
aios dev --app . --port 18080
```

In another terminal:

```bash
export AIOS_SERVER=http://127.0.0.1:18080      # PowerShell: $env:AIOS_SERVER = "http://127.0.0.1:18080"
aios run my-shop "What can you help me with?"
```

You see each event as it happens, then the answer and what it cost. Look closer:

```bash
aios describe app my-shop
aios describe run <run-id> --app my-shop
aios trace <run-id> --app my-shop
```

The console shows the same, and more: start it as the [user guide](user-guide.md) says, and open
**Runs**.

## 6. Change it

The agent is plain Python. In `src/my_shop/agents.py`, change the system prompt, or have it use a
tool:

```bash
aios init tool orders.status.find --app .
```

That writes `src/my_shop/tools.py`. Let the agent use the tool, in its manifest:

```yaml
  tools: {allow: ["orders.status.find"]}
```

allow it in a policy, `policies/orders.yaml`:

```yaml
apiVersion: aios/v1alpha1
kind: Policy
metadata: {name: orders, version: 0.1.0}
spec:
  subject: {app: my-shop}
  allow:
    - {action: tool.invoke, resource: orders.status.find}
```

and call it from the agent with `await ctx.tools.invoke("orders.status.find", text="1001")`. Start
AIOS with the App's tools: `AIOS_TOOL_PROVIDERS=my_shop.tools:providers aios dev --app .`.
[Build agents](guides/agents.md) shows the rest: letting the model choose tools, memory, asking a
person.

Add an agent the same way: `aios init agent reviewer-agent --app .` (or `--template langgraph`).

## 7. Install it

On a shared AIOS, a platform administrator offers the App's code, and a tenant administrator
agrees to what it asks for:

```bash
aios app install .
aios app consent my-shop
```

Until someone agrees, nothing of it runs. What they refuse stays refused, at runtime and across
upgrades. A new version is `aios app install .` again, after raising `metadata.version`.

## Next

- [Concepts](concepts.md): runs, events, policies, approvals and people, memory, and tenants.
- [Build agents](guides/agents.md): the `ctx` an agent gets, and how to test what it does.
- [Other frameworks](guides/frameworks.md): LangGraph and the OpenAI Agents SDK.
- The [manifest reference](reference/manifests.md) and the [command reference](reference/cli.md).
