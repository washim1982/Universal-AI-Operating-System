# ADR-025: Apps for OpenAI clients and MCP clients

- **Status:** Proposed (implemented; awaiting the owner's review)
- **Date:** 2026-11-07
- **Deciders:** Tech lead, AI engineering
- **Requirements:** FR-INT-01 P1 (MCP server), FR-INT-03 (OpenAI-compatible chat), FR-APP-02 P1
  (the OpenAI-compatible entry point); ADR-011, ADR-016, ADR-017, ADR-023

## Context

WP 3.7 lets programs that already exist reach AIOS Apps without learning the AIOS API. Two kinds
matter most. OpenAI's clients and SDKs, and the many tools built on them, take a base URL, a key
and a model name. MCP clients, such as desktop assistants and IDEs, list a server's tools and call
them.

Both expect an answer back from one request. An AIOS run doesn't fit that: it can take minutes, wait
for an approval or for a person's task, or wait for a signal, and it goes on after a crash or a
restart. Both kinds of client also act for a person, and an MCP client is an AI assistant, which
could be talked into things by what it reads.

## Decision

**One way to ask and wait** (`aios.controlplane.asking`). Asking starts a run of the App's chat
entry point for the caller, then waits until the run ends, waits for someone, or a time limit
passes (`answer_wait`, 120 seconds by default). The answer is the run's output: text as it is, an
object's `answer` (or `reply`, `text`, `content`, `message`), or else its JSON. A run that waits
comes back as a message that says what it waits for (which approval, from whom; which task, for
whom; which signal) and its run id. A run is never stopped because its caller stopped waiting. The
run's state is followed from its events, reading only those it hasn't seen.

**OpenAI's chat completions, at `/openai/v1`.** `GET /models` lists the Apps the caller can chat
with, as models; `POST /chat/completions` with an App's name as the `model` is a run of that App.
The last user message is the run's `message`, and the whole conversation is its `messages`, so an
App can use the history or ignore it. The reply has OpenAI's shape, the run's token use, and an
`aios` field with the run id, its status and its trace. `stream: true` sends a first chunk, a
keep-alive comment every 10 seconds while the run works, then the answer whole, since an App
answers whole; `stream_options.include_usage` adds the usage chunk. A failed run is a 502 with the
run's error, and in a stream an error event. Errors have OpenAI's shape, so clients raise the right
exception. `X-AIOS-Session` joins an earlier run's conversation, `X-AIOS-Data-Class` labels the
run, and `traceparent` joins the caller's trace.

A request that offers its own `tools` is refused: an App answers with the tools its manifest names,
through Tool OS, and quietly dropping the client's tools would leave the client waiting for calls
that never come. Sampling settings (temperature and so on) are the App's to choose and are ignored.

**An MCP server, at `/mcp`.** Streamable HTTP, stateless (any API replica answers any request),
with replies as server-sent events, for the 2026 protocol and the classic `initialize` handshake
alike. It offers:

- a tool `ask-<app>` for each App the caller's tenant can chat with (`message`, and an optional
  `session`), described with the App's own description;
- `run-status` (`app`, `run_id`, an optional `wait_seconds`), for a run that was still working or
  waiting;
- each App's description as the resource `aios://apps/<app>`.

A tool's result is the answer as text, and the run's state (`run_id`, `status`, `output`, `error`,
`waiting`, `usage`, `trace`) as structured content. A failed run, a refusal and a bad argument are
tool errors, which the assistant sees and can explain.

**There is no tool to approve, deny or complete a person's task.** People decide in the console, in
Slack, Teams or email, or through the API with their own credential (ADR-016, ADR-023). An
assistant can say that a run waits, and for whom; it can't answer for them.

**Every request is the caller's own.** Both doors authenticate as every other route does (an API
key or an identity provider's token, ADR-011), and a policy decides: `app.read` to list, `run.start`
to ask, `run.read` for `run-status`. The run is started for the caller, so it names them in
`onbehalfof`, and each tool call is decided for the agent and for them. The App's policies,
budgets, grants, approvals and egress apply as to any run. The request event records the door
(`via: openai` or `via: mcp`), so the audit trail says how a run was asked for. Without sign-in (a
local development server, which only a loopback address may run), the MCP server answers only
requests addressed to a loopback name, so that a web page can't reach it by DNS rebinding.

## Consequences

- Any OpenAI SDK, and anything built on one, can talk to an App with a base URL and a key; any MCP
  client can use the Apps as tools. Neither needs AIOS code.
- An answer that takes longer than `answer_wait` comes back as "still working"; the caller asks
  again with the run id, by `run-status` or `GET /v1/apps/{app}/runs/{run}`. OpenAI's clients have
  no such notion, so for them the text says so.
- Streaming sends the answer whole. Token streaming is ADR-010's, and still open.
- An MCP client is an AI assistant: what it sends may carry instructions it picked up elsewhere. A
  run it starts is limited to what the credential may do, as for any caller; an App that serves
  assistants can say `untrustedInput: true` on its chat entry point, which taints its runs
  (ADR-017). Give an assistant a key of its own, with the roles it needs.
- Agents are reached through their App, since a run is where policies, budgets and traces apply.
  Exposing one agent's capability as a tool of its own, and MCP prompts, are left for later.

## Alternatives considered

- **Answering only when the run ends.** Simple, but a run waiting for an approval would hold the
  connection for hours, and most clients give up long before.
- **Mapping OpenAI's tool calls to an App's tools.** The client would then run AIOS tools itself,
  outside Tool OS, or AIOS would run the client's, outside the App's grants.
- **The high-level `MCPServer` with one decorated function per App.** Apps come and go while the
  server runs, and each caller's tenant sees its own; the low-level server lists them per request.
- **Approve and deny tools for MCP.** Convenient for one person with one assistant, but it would let
  an assistant, or whatever it read, approve what policy said a person must.
