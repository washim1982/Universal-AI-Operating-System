# ADR-024: Tool calling, framework adapters, and what developers build with

- **Status:** Proposed (implemented; awaiting the owner's review)
- **Date:** 2026-11-07
- **Deciders:** Tech lead, AI engineering
- **Requirements:** FR-MOD-01 (tool calling), FR-AGT-02, FR-INT-04 (LangGraph, OpenAI Agents SDK),
  FR-CP-03 (`describe`), FR-CP-04 (TypeScript), FR-CP-06 (templates), FR-WF-01 (YAML and JSON),
  FR-EVT-10 (schema registry, compatibility); M3 exit 1; ADR-001, ADR-008, ADR-010

## Context

WP 3.6 is what developers meet first: templates, the commands they type, a client for TypeScript,
docs, and a way to bring agents built with LangGraph or the OpenAI Agents SDK. M3 exit 1 is three
developers building and installing a new App from a template, using only the docs, in a day.

Both frameworks are built around a model that asks for tool calls. The Model Kernel had no tool
calling (FR-MOD-01 lists it, P0), so an adapter could only fake it through structured output, or
let the framework call its provider directly, past budgets, routing and recordings.

## Decision

**Tool calling is the Model Kernel's.** A request can offer `tools` (a name, a description, a JSON
Schema) and a `tool_choice`. A reply carries `tool_calls`, which each provider adapter maps to and
from its own API (Anthropic `tool_use`, OpenAI functions, Gemini function calls). The calls are
recorded on `model.call.completed` and in cassettes, so recovery and replay give them back. Asking is
all a model does: the agent makes a call through Tool OS, where policy, approvals and egress apply,
and a tool it may not use stays unused. Providers allow no dots in a tool's name, so a model sees
`kb.article.find` as `kb__article__find`. The new fields are left out of a request's dump while
empty, so every recording made before them still matches its request.

The agent's side: `ctx.model.complete(messages, tools=...)` returns the whole reply;
`ctx.tools.describe(*names)` tells a model about tools, and `ctx.tools.call(call)` makes a call
it asked for.

**Adapters host other frameworks' agents.** `runtime: {type: "adapter:langgraph"}` or
`"adapter:openai-agents"`, with an entrypoint that is the framework's object (a compiled graph, an
`Agent`) or a function that builds one. The adapter gives the framework a model that is the Model
Kernel (`ChatAIOS`; `AIOSModel` for every agent of an SDK run, handoffs included), and the AIOS
tools the manifest names exactly in `tools.allow`, as the framework's own tools that call Tool OS.
The OpenAI Agents SDK's tracing is off: AIOS traces the run, and nothing goes to OpenAI's trace
service. Tools hosted by OpenAI are refused, because they would run outside AIOS. Each framework is
an optional extra, `aios[langgraph]` and `aios[openai-agents]`, in a layer of its own between the
control plane and the components.

**Templates are folders with tests.** `aios init <app> --template chat|rag|approval|people|
langgraph|openai-agents` copies a folder written with placeholder names that are valid Python, so
the templates are linted like the rest. Each has a test that passes as made, with
`aios.testing.models.ScriptedModel`, a stand-in for every model. CI makes each template and runs
its tests. `aios init agent` and `aios init tool` add to an App.

**Commands for people:** `aios describe app|run|agent|approval|task` in words (`GET /v1/apps/{app}`
says what an App is made of), and `aios validate <folder>`, which loads an App as AIOS would,
offline, and finds what would otherwise show up only when it runs.

**YAML and JSON are one IR.** An App folder reads `*.yaml`, `*.yml` and `*.json`; a JSON file may
hold a list of manifests. A workflow in Python, YAML or JSON is the same manifest.

**The schema registry is the catalog, served.** `GET /v1/schemas` lists every event type and
manifest kind, with each JSON Schema at its own route. `aios specs compat --against <specs>` refuses
a change that would stop today's models reading what the old ones wrote: a field removed, newly
required or narrowed, a value removed, a type gone. CI runs it against the base branch of every
pull request.

**A TypeScript client with no dependencies** (`sdks/typescript`, `@aios/client`), on the platform's
`fetch`. It follows a run's server-sent events and picks up after a dropped connection. A Python test
checks that every route it calls is one the API serves.

**A docs site** (MkDocs Material): a quickstart, concepts, guides, the existing guides, and
reference pages generated from the code (`aios docs reference`, checked in CI).

## Consequences

- Native tool calling closes the FR-MOD-01 gap for every agent, not only adapted ones. Streaming
  stays open.
- An adapted agent's own Python tools are its own code, ungoverned as in any Python agent; only AIOS
  tools pass through Tool OS. The docs say so.
- Adding the frameworks moved `websockets` from 17.4 to 16.1.1 in the lock; nothing of AIOS needs 17.
- LangGraph 1.x deprecates `create_react_agent`, so the templates and docs build the loop from
  `ToolNode` and `tools_condition`.
- Material for MkDocs announces its end of life in May 2027, with Zensical as its successor, which
  reads the same `mkdocs.yml`. Moving is a later decision.
- `aios validate` imports the App's code: run it on code you trust, as you would run its tests.

## Alternatives considered

- **Tool calls through structured output.** Works with any model, but it is less reliable, and it
  would leave FR-MOD-01 open for everyone else.
- **Letting a framework call its provider itself.** Simplest, but every call would skip routing,
  budgets, data classes and recordings.
- **Generating the TypeScript client from OpenAPI.** Many routes return untyped JSON today, so a
  generated client would type little. A hand-written one, checked against the routes, types what
  matters.
