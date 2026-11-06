# ADR-017: Taint goes on the envelope, and a person's own request doesn't set it

- **Status:** Proposed (implemented; awaiting the owner's review)
- **Date:** 2026-11-01
- **Deciders:** Tech lead
- **Requirements:** FR-SEC-09, 10, 11; FR-MOD-06; P8; M2 exit 3 and 6

## Context

Taint used to live in one task's memory. A knowledge-base article read in the Support Desk's
`research` step left the run clean by the time the `resolve` step issued a refund, so M2 exit 3
could not pass.

A data label had a quieter version of the same problem. Labels follow the event that caused the
next one, and a fan-out join is caused by whichever item finished last. An earlier item's
`restricted` label could vanish there, and restricted content could then reach a public model.

Two things have to be decided. Where taint lives, and what counts as untrusted, because the
requirements say that "user input" does (P8, WP 2.3), and taken literally that makes every run
tainted, and every refund a person's job.

## Decision

**Taint is an envelope attribute, beside `dataclass`, and it only ever turns on.** `derive` keeps
it, the kernel's replies carry it, and `ctx.emit` carries it. So it travels with events into runs
and subscribers that have no other link to where it started. A `RunState` field wouldn't have
stopped a tainted task from emitting an event for a clean agent to act on.

**A run's taint and label are folded from its whole log.** The engine stamps the fold on everything
it publishes, not what the triggering event happened to carry, which closes the fan-in gap for both.
A parallel task already running isn't reached, and needn't be: it hasn't seen the content. If it
reads something another task wrote, memory taints it then.

**What sets it:** a tool that isn't marked `trusted`, memory that returned something, an event
entrypoint unless it says `untrustedInput: false`, a chat App that says `untrustedInput: true`, and
a classifier that flags content, wherever it came from.

**A person's own request doesn't set it.** This narrows the literal requirement, and it is the
decision for the owner to check. A signed-in caller is the principal the run acts for. Policy
already limits what they can ask for, and the agent gets the intersection of both permissions, so
direct injection is controlled by policy (requirements §5.8 says so too). Tainting every run would
send every financial and outbound action to a person, whatever anyone said, and taint would stop
meaning anything. A request that looks like an injection does taint its run, and a chat App whose
callers relay strangers' words says so in its manifest.

**Sources are recorded,** as `tool:<name>`, `memory:<id>`, `input` and `suspected:<source>`, and an
approver sees them. FR-SEC-09 asks for provenance, and "the agent read something" isn't much to
decide on.

**An App can accept the risk, in writing.** `spec.taint.exempt` names tools that need no approval
while tainted (FR-SEC-09's "configurable per policy"). Without it every Support Desk reply would need
a person. The events still say the context was tainted.

**A tool's output is untrusted unless the tool says otherwise**, including what a write tool
returns. That means a second refund in a run needs a person: the first one's answer came from
outside. Native tools that produce their own output, such as `filesystem.file.write`, are `trusted`.

**The classifier is a signal.** It sits at `ctx`, where outside content enters an agent's context,
and reads chat input in `Runtime.chat`. It records `security.injection.suspected`, which never
quotes the content, and taints the run. It can't block anything, and if it fails, work carries on.
Nothing relies on it (risk R5): the corpus passes with it off, apart from DI-03.

**Egress is a list per App, narrowed per tool, under an installation ceiling.** A host has to be on
every list that exists, and with none at all nothing is reachable. Once an App declares a list,
every URL anywhere in every tool's arguments is checked, because that is how a fooled agent leaks
data. An approval can't lift it: nobody should be asked to wave a request through to a host that
isn't listed.

**`restricted` data can go to `private` or `zero-retention` deployments** (FR-MOD-06), and routing
uses the strictest of the request, what the agent has read and the run. A caller can't understate a
request's label: memory search used to embed a query under the event's label, whatever the agent
had read by then.

## Alternatives considered

| Option | Why not |
|---|---|
| Taint only in the run's fold | A subscriber, or a run started by an event, has no fold to read, so a tainted task could launder through `ctx.emit` |
| Check the log on every tool call | It would also catch tasks already in flight, which haven't seen the content. It costs a query per call for no safety |
| Taint by data flow, step by step | More precise, and it would reduce approvals. It needs to know what each agent passed on, and memory is a side channel. Run-wide is what FR-SEC-09 says |
| Every request taints | The literal reading: see above |
| Block on the classifier | One phrase list deciding what agents may read. False positives would stop real work, and it would look like a control when it isn't one |
| Allow `*` in egress lists | An App that wants everywhere has no list. A wildcard would look like one |
| Scan all arguments with no App list | It would refuse any link in any message, on every App that never asked for it |

## Consequences

- **Approvals go up.** Any run that reads something and then does something risky asks a person,
  which is the point, but approval fatigue is a real risk. `spec.taint.exempt` is the release valve.
- Marking a tool `trusted` is now a security decision: its output can't taint a run.
- A task's handler can no longer set `ctx.tainted`. It stops mistakes, not a hostile handler, which
  needs out-of-process agents (ADR-014).
- Events gained an attribute, which ADR-002 says is a compatible change. Events stored before it
  read as clean.
- `AIOS_HTTP_ALLOWED_DOMAINS` is now a ceiling on what Apps declare.

## Implementation

`aios.core.envelope`, `aios.workflow.state` (the fold), `aios.kernel.context` (`taint`, `admit`,
`quote`), `aios.security.injection`, `aios.tools.egress`, `aios.tools.service._refused` and
`aios.models.kernel._labelled`.

Proof: `tests/redteam/` runs the corpus on the real runtime, assuming the model is fooled every
time, and [threat-model.md](../threat-model.md) maps each threat to its cases.
