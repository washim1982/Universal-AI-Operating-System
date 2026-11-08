# AIOS

AIOS is an operating system for AI agents. You build an **App**: agents, the steps they take,
and the rules they follow. AIOS runs it for you, and for every action an agent takes it answers
four questions:

- **Who may do this?** Policies decide every tool call, memory read and model call, per tenant,
  App and agent. A person can be asked first, and must agree before the action happens.
- **What will it cost?** Each model call is routed by policy, priced and charged against budgets
  that stop a run before it overspends.
- **What happened?** A run is the events it made: every model call, tool call, decision and
  answer. You can trace it, replay it for free, fork it from a step, and grade it.
- **What did it read?** Content from the web or from people outside is marked, and an agent that
  has read it can't use risky tools without someone checking.

## Where to start

<div class="grid cards" markdown>

- **Build an App**: [Quickstart](quickstart.md), then [Build agents](guides/agents.md).
  Bring an agent you built with LangGraph or the OpenAI Agents SDK: [Other frameworks](guides/frameworks.md).
- **Understand how it fits together**: [Concepts](concepts.md).
- **Use, run or approve AI Apps**: the [user guide](user-guide.md), by role.
- **Call AIOS from a program**: the [API](reference/api.md), the Python CLI, or the
  [TypeScript client](guides/typescript.md).
- **Use an App from an OpenAI client or an AI assistant**: [OpenAI clients and MCP](guides/clients.md).
- **Check that it works**: the [testing guide](testing-guide.md).

</div>

## What you need

Python 3.12 or later, and [uv](https://docs.astral.sh/uv/) or pip. PostgreSQL (with pgvector) runs
AIOS for real; on one machine, `aios dev` needs nothing else. A model provider's key (Anthropic,
OpenAI, Google) or a model on your own server, for Apps that use models; their tests don't.
