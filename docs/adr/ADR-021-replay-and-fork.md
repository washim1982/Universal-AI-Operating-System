# ADR-021: Replay, fork and retry are new runs that answer from their source's record

- **Status:** Proposed (implemented; awaiting the owner's review)
- **Date:** 2026-11-02
- **Deciders:** Tech lead
- **Requirements:** FR-OBS-04 (replay and fork), FR-WF-11 (retry and fork from a step),
  FR-CP-03 (`aios replay`), FR-UI-03 (replay and fork in the console); ADR-002, ADR-006, ADR-017

## Context

A run is a sequence of events (ADR-006), so the Trace Explorer can already redraw any past run.
FR-OBS-04 asks for more. Someone debugging an agent wants to run it again and get the same answer
without paying for it or doing anything twice. They also want to change one thing (the input, a
step's input, the model, or the agent's code) and see what changes from that step on, without
refunding a customer a second time. An operator wants to start a failed run again from the step
that failed, for real (FR-WF-11).

Each model and tool call already has a *site*: step, item, agent, loop, attempt, kind and index
(`research#0/researcher/l0a1/agent/model/2`). The site doesn't contain the run id, so the same
call has the same site in a second run of the same workflow.

ADR-006 rules out replaying code to rebuild a run's state: the engine folds events instead. This
ADR doesn't change that. A replay is a new run whose calls are answered from the old one's
events; nothing is rebuilt by running code.

## Decision

**Replay, fork and retry each start a new run.** They never change the old one, whose events stay
as they are. The new run's `app.request.received` and `run.started` carry a `source`: the run it
comes from, the mode, the step it starts again from, and what it changes. It has its own events,
audit trail and costs, and the run list, the API and the console say where it came from. Only a
finished run can be the source. A running run's record isn't complete.

**A replay answers every call from the source's record, matched by site.** A model call gets the
source's `model.call.completed` for the same site, marked `replayed`, at no cost. A tool call gets
the source's output, or its error again with the same code. Nothing is called. Matching by site
rather than by order means parallel steps and `foreach` items replay correctly whatever order they
finish in. A replay is a new run, so the one thing allowed to differ from the record is the run's
own id. An agent puts it in a file path, a prompt or an idempotency key (`ctx.step_key`), and
where the new run's id appears, the source's id is read in its place before comparing. An agent
is handed a tool's output as the record holds it, live or replayed: JSON, with every object's
keys sorted. PostgreSQL's `jsonb` doesn't keep key order, so without this, a prompt built from a
tool's output on PostgreSQL would differ in a replay and diverge. The real-stack check found it.
The in-memory store keeps key order, so the in-memory tests couldn't.

**A replay stops where it can't answer from the record.** That can be a call the source never
made, a tool call with other arguments, or a model request whose hash differs from the source's
(new prompt, new memory content, new agent code). The run then fails with `replay.diverged`,
which names the site. It never goes on with a live call: a replay that quietly spent money or
did something would no longer be a replay.

**A fork keeps everything outside the subtree of the step it starts from.** Those steps'
results are copied as `step.completed` with `copied_from`, along with the taint they carried
(ADR-017). A fork of a run that read something untrusted is tainted too. Only steps that
completed are kept: one that failed or never ran in the source runs again. The start step and
everything that depends on it run again, live, on the source's workflow version and with the
agent code installed now. A fork can change the run's input, the start step's input, and the model
(`model`, a registry entry every call must use, through the router).

**A fork holds back side effects.** These are any tool that isn't `READ`, memory writes, updates
and erasures, and events the agent emits. A held-back tool call returns the source's output if the
source made the same call at that site, and `{"stubbed": true, "tool": ...}` if it didn't. Either
way it is as untrusted as the tool. Memory writes and updates are recorded with `stubbed`, and
nothing is stored or erased. An emit becomes `agent.emit.stubbed`. Policy still
decides each held-back call. A denied call fails as it would have. A call that would need an
approval is recorded as `escalated`, and nobody is asked, because nothing happens.

**People and signals aren't asked again in a replay or fork.** When the run reaches an approval
or wait step, the source's answer is given (`source.answers`) at that point in the run, not up
front.

**A retry is a fork with nothing held back.** It starts by default at the step that failed, and
it asks people again. It does things for real, so it needs `run.control` (operator). Replay and
fork need only `run.start`, because neither does anything outside AIOS.

**Two runs are compared step by step.** `GET /v1/apps/{app}/runs/{a}/compare/{b}` returns each
step of both runs with its status, output, cost and tokens, whether it was copied, how many side
effects were held back, and whether outputs match. `aios compare` prints it and exits 1 when the
outputs differ. The console shows it beside both runs.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Redraw the trace only | The Trace Explorer already does this. It can't show whether the run is repeatable, and it can't fork |
| Match recorded calls by their order | Parallel steps and `foreach` items finish in any order |
| Fall back to a live call on divergence | It would spend money and could do things, without saying so |
| Rewrite the source run in place | Events are a record, and the audit trail depends on them |
| Snapshot engine state at step N | Engine state is a fold over events. Copied `step.completed` events are that snapshot, and they show in the trace |
| Hold back side effects in a retry too | A retry exists to do the failed work for real |
| Ask approvers again in a fork | Nothing a fork does reaches the world, so there is nothing to approve |

## Consequences

- A replay costs nothing and shows whether a run is repeatable. A divergence says which call
  changed.
- Memory reads in a replay or fork are live, not taken from the record. Recall falls back to
  keywords because a replay makes no embeddings. If what an agent recalls changes its prompt, the
  request hash catches it as a divergence. It never passes unnoticed.
- Agent code isn't pinned per version. A fork runs the code installed now, so a fork after an
  upgrade *is* a fork with the new agent version. Forking with an older version waits on agents
  running in their own processes (ADR-014).
- Model calls recorded before `request_hash` existed replay by site only, so a changed prompt
  there isn't detected.
- Every agent now gets tool outputs with their keys sorted, in live runs too. JSON objects have
  no order, but an agent that relied on a tool's key order would see a change.
- A held-back call the source never made returns a placeholder. An agent that depends on its
  output may fail, and that is the fork's honest result.
- New events: `agent.emit.stubbed`. New fields: `source` on `app.request.received` and
  `run.started`, `copied_from` on `step.completed`, `replayed` and `request_hash` on
  `model.call.completed`, `stubbed` and `replayed` on `tool.call.completed`, and `stubbed` on
  `memory.item.written`.
