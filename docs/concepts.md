# Concepts

## Apps, agents and workflows

An **App** is what a tenant installs. It is a folder of manifests (YAML or JSON) and code:

- **Agents** do work. Each provides **capabilities**, such as `research.web`, and is Python code
  (`runtime: {type: python}`), or an agent built with another framework
  (`runtime: {type: "adapter:langgraph"}`).
- **People** can provide a capability too (`spec.humans`): its work waits in their **Tasks**.
- **Workflows** are the steps of a run: tasks, tool calls, approvals, waits for a signal,
  fan-out over a list, loops back to an earlier step, all with budgets and timeouts. A workflow
  written in Python (`aios.sdk.Workflow`), YAML or JSON is the same graph.
- **Policies** say what the App's agents may do, and when a person must agree first.
- **Entry points** start runs: a chat message, an event, a schedule or a signed webhook.

## Runs are events

A **run** is one execution of a workflow, and it *is* its events: `run.started`, each step, each
model and tool call, each decision and answer, `run.completed`. Nothing about a run lives only in
memory:

- A run that waits for a person uses no compute, and survives restarts and deployments.
- A worker that crashes mid-task is replaced, and the task carries on without repeating what it
  did: calls already recorded are answered from the record. A call whose answer the worker hadn't
  stored when it died is made again, once.
- You can **replay** a run (every call answered from the record, at no cost), **fork** it from a
  step with a different input or model (with side effects held back), or **retry** it for real.
- Every event has the same envelope (CloudEvents 1.0 and AIOS's fields: tenant, App, run, who acted,
  for whom, how sensitive, whether it read untrusted content). The [events](reference/events.md)
  are a public contract, with a schema each (`/v1/schemas`).

## Every action is decided

An agent never calls a model, a tool or memory directly. It asks through its `ctx`, and the
platform decides:

- **Policies** (compiled to Cedar) decide each tool call, memory read and write, and model route,
  for the agent and for whoever it acts for. Anything no rule allows is refused.
- **Approvals**: a rule can ask a person, a group or several people first. The run pauses; the
  answer comes from the console, the CLI, Slack, Teams or email. The approver sees exactly what
  would happen, a dry run where the tool offers one, and may change it; a change is checked again.
- **Budgets** cap what a run, a user, an App and a tenant spend. A soft limit moves to a cheaper
  model; a hard one stops the call.
- **Data classes** (`public` to `restricted`) follow data through a run: restricted data only
  reaches models a policy approves.
- **Taint**: once an agent has read something from outside (a web page, an email, a tool's output),
  risky tools need a person, because what it read may be instructions from someone else.
- The **kill switch** stops an agent, an App or a tenant at once.

## Models and tools

The **Model Kernel** routes each model call by policy (task, data class, budget) to a model in the
registry: Anthropic, OpenAI, Google, or any OpenAI-compatible server such as your own. It falls
back when one fails, prices every call, and records it for replay. A model may ask for tool calls;
the agent makes them, through the **Tool OS**, where policy and approvals apply.

Tools are native Python functions, or come from MCP servers. A tool says what kind of side effect
it has (read, write, financial...), and policies can treat each kind differently.

## Memory

**Memory OS** keeps what agents and people put there, by scope: a run's working memory, a
conversation's session, a user's, an agent's, an App's (its documents), a tenant's. Search is by
keyword and by meaning, returns cited, deduplicated context within a token budget, and is decided
by policy like everything else. Erasing a person erases them everywhere.

## People in the loop

People approve, do steps (`spec.humans`), grade evaluations, and give feedback on runs. Each of
these is an event on the run, so it is in the trace and the audit log. Feedback with a correction
can become an evaluation case.

## Tenants and roles

A **tenant** is one organisation. Its data, runs and events are kept apart from every other
tenant's, down to the database's row-level security. People have **roles**: `member`, `auditor`,
`operator`, `tenant-admin`, and `platform-admin` for whoever runs the installation. Who may do
what is in the [user guide](user-guide.md).

## Evaluation

An **Evaluation** is a suite of cases, graders (rules, schemas, a model as judge, people) and a
gate. `aios eval run` runs each case as a run of the App and fails when a change scores below its
baseline, so CI can block a prompt change that makes answers worse.
